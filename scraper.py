import json
import time
import re
import html
import os
import base64
import requests
from nacl import encoding, public as nacl_public

# ─── CONFIGURATION ─────────────────────────────────────────────────────────────
LI_AT_COOKIE         = os.environ.get("LINKEDIN_LI_AT", "")
LINKEDIN_PROFILE_URL = os.environ.get("LINKEDIN_PROFILE_URL", "")
GH_PAT               = os.environ.get("GH_PAT", "")
GITHUB_REPOSITORY    = os.environ.get("GITHUB_REPOSITORY", "")
# ──────────────────────────────────────────────────────────────────────────────


def extract_username_from_url(url: str) -> str:
    match = re.search(r'linkedin\.com/in/([^/?#]+)', url)
    return match.group(1).strip('/') if match else ""


# ═══════════════════════════════════════════════════════════════
# GITHUB SECRET AUTO-REFRESH
# ═══════════════════════════════════════════════════════════════

def _encrypt_secret(pub_key_b64: str, value: str) -> str:
    pk  = nacl_public.PublicKey(pub_key_b64.encode(), encoding.Base64Encoder())
    box = nacl_public.SealedBox(pk)
    return base64.b64encode(box.encrypt(value.encode())).decode()


def refresh_github_secret(secret_name: str, new_value: str) -> bool:
    if not GH_PAT or not GITHUB_REPOSITORY:
        return False
    headers = {
        "Authorization": f"Bearer {GH_PAT}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
    }
    base = f"https://api.github.com/repos/{GITHUB_REPOSITORY}/actions/secrets"
    kr   = requests.get(f"{base}/public-key", headers=headers, timeout=10)
    if kr.status_code != 200:
        return False
    kd   = kr.json()
    resp = requests.put(
        f"{base}/{secret_name}",
        headers=headers,
        json={"encrypted_value": _encrypt_secret(kd["key"], new_value), "key_id": kd["key_id"]},
        timeout=10,
    )
    if resp.status_code in (201, 204):
        print(f"   ✅ Secret '{secret_name}' auto-refreshed")
        return True
    return False


# ═══════════════════════════════════════════════════════════════
# PAYLOAD PARSER (HTML <code> EMBEDDED JSON)
# ═══════════════════════════════════════════════════════════════

def _extract_text_from_item(item: dict) -> str:
    # 1. Commentary
    commentary = item.get("commentary")
    if isinstance(commentary, dict):
        text_obj = commentary.get("text")
        if isinstance(text_obj, dict):
            t = text_obj.get("text")
            if isinstance(t, str):
                return t.strip()
        elif isinstance(text_obj, str):
            return text_obj.strip()

    # 2. Description
    desc = item.get("description")
    if isinstance(desc, dict):
        t = desc.get("text")
        if isinstance(t, str):
            return t.strip()
    elif isinstance(desc, str):
        return desc.strip()

    # 3. Direct text field
    text_val = item.get("text")
    if isinstance(text_val, dict):
        t = text_val.get("text")
        if isinstance(t, str):
            return t.strip()
    elif isinstance(text_val, str):
        return text_val.strip()

    return ""


def _extract_date_from_item(item: dict) -> str:
    for k in ("publishedAt", "createdAt", "postedAt", "created", "timestamp"):
        val = item.get(k)
        if isinstance(val, (int, float)) and val > 1_000_000_000:
            return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(val / 1000))
        elif isinstance(val, str) and len(val) > 5:
            return val
    return ""


def _extract_image_from_item(item: dict) -> str:
    content = item.get("content")
    if isinstance(content, dict):
        url = content.get("url") or content.get("rootUrl")
        if url:
            return str(url)
        images = content.get("images")
        if isinstance(images, list) and len(images) > 0:
            img = images[0]
            if isinstance(img, dict):
                return img.get("url") or img.get("rootUrl", "")
    return ""


def _extract_post_url_from_item(item: dict, username: str) -> str:
    entity_urn = item.get("entityUrn") or item.get("urn") or ""
    if "activity:" in entity_urn or "share:" in entity_urn:
        act_id = entity_urn.split(":")[-1]
        return f"https://www.linkedin.com/feed/update/urn:li:activity:{act_id}/"
    return f"https://www.linkedin.com/in/{username}/recent-activity/all/"


def parse_linkedin_html_payload(html_content: str, username: str) -> list[dict]:
    posts = []
    seen_texts = set()

    # Find all <code>...</code> blocks in the HTML
    code_blocks = re.findall(r'<code[^>]*>(.*?)</code>', html_content, re.DOTALL)
    print(f"   Found {len(code_blocks)} <code> tags in response HTML")

    # ── DIAGNOSTIC: dump every code block to file AND stdout ──────────────
    debug_lines = []
    for i, raw_block in enumerate(code_blocks):
        cleaned = raw_block.strip()
        if cleaned.startswith("<!--") and cleaned.endswith("-->"):
            cleaned = cleaned[4:-3].strip()
        cleaned = html.unescape(cleaned)
        # Print to log for instant inspection
        preview = cleaned[:300].replace('\n', ' ')
        print(f"   [Block {i:02d}] len={len(cleaned):6d}  starts: {preview[:120]}")
        debug_lines.append(f"=== CODE BLOCK {i} (len={len(cleaned)}) ===")
        debug_lines.append(cleaned[:2000])
        debug_lines.append("")

    with open("code_tags_debug.txt", "w", encoding="utf-8") as f:
        f.write("\n".join(debug_lines))
    print(f"   📄 Saved code_tags_debug.txt")

    # Save first 200KB of raw HTML
    with open("raw_response.html", "w", encoding="utf-8") as f:
        f.write(html_content[:200_000])
    print(f"   📄 Saved raw_response.html (first 200KB of {len(html_content)} bytes)")
    # ── END DIAGNOSTIC ─────────────────────────────────────────────────────


    for idx, raw_block in enumerate(code_blocks):
        cleaned = raw_block.strip()

        # Strip HTML comment wrappers <!-- ... -->
        if cleaned.startswith("<!--") and cleaned.endswith("-->"):
            cleaned = cleaned[4:-3].strip()

        cleaned = html.unescape(cleaned)

        if not (cleaned.startswith("{") or cleaned.startswith("[")):
            continue

        try:
            data = json.loads(cleaned)
        except Exception:
            continue

        # Build the list of items to inspect for post content.
        # LinkedIn's code blocks use several formats:
        #   1. Voyager REST:  {"data": {...}, "included": [...]}
        #   2. Collection:    {"data": {"elements": [...], ...}}
        #   3. GraphQL:       {"data": {"data": {"identityDash...": {...}}}}
        items = []

        if isinstance(data, dict):
            top_data = data.get("data", {})

            # Format 1: top-level included array
            if "included" in data and isinstance(data["included"], list):
                items.extend(data["included"])

            # Format 2: data.elements array (Block 00 — the activity collection)
            if isinstance(top_data, dict) and "elements" in top_data:
                elems = top_data["elements"]
                if isinstance(elems, list):
                    items.extend(elems)

            # Format 3: data.data (GraphQL double-wrapper, Blocks 14 & 16)
            if isinstance(top_data, dict) and "data" in top_data:
                inner = top_data["data"]
                if isinstance(inner, dict):
                    items.append(inner)
                    # Recursively extract any nested elements/included
                    for v in inner.values():
                        if isinstance(v, dict) and "elements" in v:
                            items.extend(v["elements"])
                        if isinstance(v, list):
                            items.extend(v)

            # Also treat top_data itself as an item
            if isinstance(top_data, dict):
                items.append(top_data)

        elif isinstance(data, list):
            items.extend(data)

        for item in items:
            if not isinstance(item, dict):
                continue

            text = _extract_text_from_item(item)

            # Deeper search: look inside nested value/content dicts
            if not text:
                for nested_key in ("value", "actor", "content", "updateMetadata"):
                    nested = item.get(nested_key)
                    if isinstance(nested, dict):
                        text = _extract_text_from_item(nested)
                        if text:
                            break

            if not text or len(text) < 20 or text in seen_texts:
                continue

            # Skip non-post strings
            skip_prefixes = (
                "Web Developer", "Experience", "Education", "Contact info",
                "urn:", "com.linkedin", "http", "AQ", "AAZ",
            )
            if any(text.startswith(p) for p in skip_prefixes):
                continue

            seen_texts.add(text)
            date      = _extract_date_from_item(item)
            image_url = _extract_image_from_item(item)
            post_url  = _extract_post_url_from_item(item, username)

            posts.append({
                "text":      text,
                "date":      date,
                "image_url": image_url,
                "post_url":  post_url,
            })

            if len(posts) >= 6:
                break

        if len(posts) >= 6:
            break

    return posts


# ═══════════════════════════════════════════════════════════════
# SINGLE DIRECT REQUEST SCRAPER
# ═══════════════════════════════════════════════════════════════

def fetch_posts_direct(username: str) -> list[dict]:
    session = requests.Session()

    headers = {
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/125.0.0.0 Safari/537.36"
        ),
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,image/apng,*/*;q=0.8",
        "Accept-Language": "en-US,en;q=0.9",
        "Accept-Encoding": "gzip, deflate, br",
        "Cache-Control": "max-age=0",
        "Sec-Ch-Ua": '"Google Chrome";v="125", "Chromium";v="125", "Not.A/Brand";v="24"',
        "Sec-Ch-Ua-Mobile": "?0",
        "Sec-Ch-Ua-Platform": '"Windows"',
        "Sec-Fetch-Dest": "document",
        "Sec-Fetch-Mode": "navigate",
        "Sec-Fetch-Site": "same-origin",
        "Sec-Fetch-User": "?1",
        "Upgrade-Insecure-Requests": "1",
    }

    if LI_AT_COOKIE:
        session.cookies.set("li_at", LI_AT_COOKIE, domain=".linkedin.com")
        print("   ✅ Session cookie set")

    target_urls = [
        f"https://www.linkedin.com/in/{username}/recent-activity/all/",
        f"https://www.linkedin.com/in/{username}/",
    ]

    posts = []
    for url in target_urls:
        print(f"   GET {url} ...")
        try:
            resp = session.get(url, headers=headers, allow_redirects=True, timeout=30)
            print(f"   Response status: {resp.status_code} | Final URL: {resp.url}")

            # Check if cookie was updated in response
            for cookie in session.cookies:
                if cookie.name == "li_at" and cookie.value and cookie.value != LI_AT_COOKIE:
                    print("   🔄 Session cookie updated by server — auto-refreshing secret ...")
                    refresh_github_secret("LINKEDIN_LI_AT", cookie.value)
                    break

            if resp.status_code == 200:
                posts = parse_linkedin_html_payload(resp.text, username)
                if posts:
                    print(f"   ✅ Successfully extracted {len(posts)} posts from {url}")
                    break
        except Exception as e:
            print(f"   ⚠️ Request notice: {e}")

    return posts


# ═══════════════════════════════════════════════════════════════
# MAIN SCRAPE EXECUTION
# ═══════════════════════════════════════════════════════════════

def scrape() -> None:
    if not LINKEDIN_PROFILE_URL:
        raise ValueError("LINKEDIN_PROFILE_URL secret is not set.")

    username = extract_username_from_url(LINKEDIN_PROFILE_URL)
    if not username:
        raise ValueError(f"Cannot extract username from: {LINKEDIN_PROFILE_URL}")

    print("🚀 Starting LinkedIn scraper (Direct Single Request) …")
    print(f"   Target Username: {username}")

    posts = fetch_posts_direct(username)

    output = {
        "updated_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "post_count": len(posts),
        "posts":      posts,
    }
    with open("posts.json", "w", encoding="utf-8") as f:
        json.dump(output, f, ensure_ascii=False, indent=2)

    print(f"\n✅ Done! Scraped {len(posts)} posts → posts.json")


if __name__ == "__main__":
    scrape()
