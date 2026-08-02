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

BROWSER_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/125.0.0.0 Safari/537.36"
    ),
    "Accept-Language":           "en-US,en;q=0.9",
    "Accept-Encoding":           "gzip, deflate, br",
    "Sec-Ch-Ua":                 '"Google Chrome";v="125", "Chromium";v="125", "Not.A/Brand";v="24"',
    "Sec-Ch-Ua-Mobile":          "?0",
    "Sec-Ch-Ua-Platform":        '"Windows"',
    "Upgrade-Insecure-Requests": "1",
}


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
# VOYAGER API PARSER
# ═══════════════════════════════════════════════════════════════

def _extract_text_from_item(item: dict) -> str:
    # 1. Commentary (standard post text field)
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
    entity_urn = item.get("entityUrn") or item.get("urn") or item.get("*urn") or ""
    if "activity:" in entity_urn or "share:" in entity_urn or "ugcPost:" in entity_urn:
        act_id = entity_urn.split(":")[-1]
        return f"https://www.linkedin.com/feed/update/urn:li:activity:{act_id}/"
    return f"https://www.linkedin.com/in/{username}/recent-activity/all/"


SKIP_PREFIXES = (
    "Web Developer", "Experience", "Education", "Contact info",
    "urn:", "com.linkedin", "http", "AQ", "AAZ", "ajax:",
)


def _is_valid_post_text(text: str) -> bool:
    if len(text) < 20:
        return False
    if any(text.startswith(p) for p in SKIP_PREFIXES):
        return False
    return True


def parse_voyager_response(data: dict, username: str) -> list[dict]:
    """
    Parse a Voyager API normalized JSON response.
    Format: {"data": {..., "elements": [...]}, "included": [...]}
    """
    posts = []
    seen = set()

    items = []

    if not isinstance(data, dict):
        return posts

    # Top-level included (Voyager REST format)
    if "included" in data and isinstance(data["included"], list):
        items.extend(data["included"])

    top = data.get("data", {})
    if isinstance(top, dict):
        # data.elements (collection response)
        for elem in top.get("elements", []):
            if isinstance(elem, dict):
                items.append(elem)
        # data.data (GraphQL double-wrapper)
        inner = top.get("data")
        if isinstance(inner, dict):
            items.append(inner)
            for v in inner.values():
                if isinstance(v, dict) and "elements" in v:
                    items.extend(v.get("elements", []))
                elif isinstance(v, list):
                    items.extend(v)

    for item in items:
        if not isinstance(item, dict):
            continue

        text = _extract_text_from_item(item)

        # Deeper search in nested dicts
        if not text:
            for k in ("value", "actor", "content", "updateMetadata"):
                nested = item.get(k)
                if isinstance(nested, dict):
                    text = _extract_text_from_item(nested)
                    if text:
                        break

        if not text or text in seen or not _is_valid_post_text(text):
            continue

        seen.add(text)
        posts.append({
            "text":      text,
            "date":      _extract_date_from_item(item),
            "image_url": _extract_image_from_item(item),
            "post_url":  _extract_post_url_from_item(item, username),
        })

        if len(posts) >= 6:
            break

    return posts


# ═══════════════════════════════════════════════════════════════
# SESSION & CSRF SETUP
# ═══════════════════════════════════════════════════════════════

def build_session() -> requests.Session:
    s = requests.Session()
    s.cookies.set("li_at", LI_AT_COOKIE, domain=".linkedin.com")
    return s


def get_csrf_token(session: requests.Session) -> str:
    """
    Load the LinkedIn feed page to receive JSESSIONID cookie.
    LinkedIn's CSRF token = JSESSIONID cookie value (without quotes).
    """
    print("   Loading feed page to obtain CSRF token …")
    try:
        resp = session.get(
            "https://www.linkedin.com/feed/",
            headers={**BROWSER_HEADERS, "Accept": "text/html,application/xhtml+xml,*/*;q=0.8"},
            timeout=30,
        )
        print(f"   Feed status: {resp.status_code}")

        # Auto-refresh if li_at changed
        for c in session.cookies:
            if c.name == "li_at" and c.value and c.value != LI_AT_COOKIE:
                refresh_github_secret("LINKEDIN_LI_AT", c.value)
                break

    except Exception as e:
        print(f"   ⚠️ Feed load notice: {e}")

    for c in session.cookies:
        if c.name == "JSESSIONID":
            return c.value.strip('"')

    return ""


# ═══════════════════════════════════════════════════════════════
# VOYAGER API CALL
# ═══════════════════════════════════════════════════════════════

def voyager_api_headers(csrf: str, referer: str) -> dict:
    return {
        **BROWSER_HEADERS,
        "Accept":                     "application/vnd.linkedin.normalized+json+2.1",
        "Accept-Language":            "en-US,en;q=0.9",
        "csrf-token":                 csrf,
        "x-restli-protocol-version":  "2.0.0",
        "x-li-lang":                  "en_US",
        "x-li-track": json.dumps({
            "clientVersion": "1.13.13993",
            "mpVersion":     "1.13.13993",
            "osName":        "web",
            "timezoneOffset": -5,
            "timezone":      "America/New_York",
            "deviceFormFactor": "DESKTOP",
            "mpName":        "voyager-web",
            "displayDensity": 1,
            "displayWidth":   1366,
            "displayHeight":  768,
        }),
        "Sec-Fetch-Dest": "empty",
        "Sec-Fetch-Mode": "cors",
        "Sec-Fetch-Site": "same-origin",
        "Referer":        referer,
        "Origin":         "https://www.linkedin.com",
    }


def fetch_posts_via_api(session: requests.Session, username: str, csrf: str) -> list[dict]:
    """
    Call Voyager API endpoints directly.
    Tries two endpoints: profileUpdatesV2 (REST) and the GraphQL activity query.
    """
    referer = f"https://www.linkedin.com/in/{username}/recent-activity/all/"
    hdrs    = voyager_api_headers(csrf, referer)

    # ── Endpoint 1: profileUpdatesV2 (classic REST) ──────────────────────
    url1    = "https://www.linkedin.com/voyager/api/identity/profileUpdatesV2"
    params1 = {"memberIdentity": username, "count": 6, "start": 0, "includeLongTermHistory": "true"}
    print(f"\n   Calling Voyager REST: {url1}")
    try:
        r = session.get(url1, headers=hdrs, params=params1, timeout=30)
        print(f"   Status: {r.status_code}")
        with open("voyager_response.json", "w") as f:
            f.write(r.text[:50_000])

        if r.status_code == 200:
            posts = parse_voyager_response(r.json(), username)
            if posts:
                return posts
        elif r.status_code == 429:
            print("   ⚠️ 429 Rate-limited — account needs more trust (see note below)")
        else:
            print(f"   ⚠️ Unexpected status {r.status_code}")
    except Exception as e:
        print(f"   ⚠️ REST API error: {e}")

    # ── Endpoint 2: GraphQL activity ──────────────────────────────────────
    url2 = (
        "https://www.linkedin.com/voyager/api/graphql"
        "?variables=(profileIdentity:(vanityName:" + username + "),count:6,start:0)"
        "&queryId=voyagerIdentityDashProfileUpdates.4a16ce71b8b9c0bfbf35abe10f75e8f8"
    )
    print(f"\n   Calling Voyager GraphQL: {url2[:90]}…")
    try:
        r2 = session.get(url2, headers={**hdrs, "Accept": "application/json"}, timeout=30)
        print(f"   Status: {r2.status_code}")
        with open("voyager_graphql_response.json", "w") as f:
            f.write(r2.text[:50_000])

        if r2.status_code == 200:
            posts = parse_voyager_response(r2.json(), username)
            if posts:
                return posts
        elif r2.status_code == 429:
            print("   ⚠️ 429 Rate-limited on GraphQL too")
    except Exception as e:
        print(f"   ⚠️ GraphQL API error: {e}")

    return []


# ═══════════════════════════════════════════════════════════════
# MAIN
# ═══════════════════════════════════════════════════════════════

def scrape() -> None:
    if not LI_AT_COOKIE:
        raise ValueError("LINKEDIN_LI_AT secret is not set.")
    if not LINKEDIN_PROFILE_URL:
        raise ValueError("LINKEDIN_PROFILE_URL secret is not set.")

    username = extract_username_from_url(LINKEDIN_PROFILE_URL)
    if not username:
        raise ValueError(f"Cannot extract username from: {LINKEDIN_PROFILE_URL}")

    print("🚀 Starting LinkedIn scraper (Voyager API via Python requests) …")
    print(f"   Target Username: {username}")

    session = build_session()
    csrf    = get_csrf_token(session)

    if not csrf:
        print("   ⚠️ No CSRF token obtained — session cookie may be invalid")
    else:
        print(f"   ✅ CSRF token obtained: {csrf[:20]}…")

    posts = fetch_posts_via_api(session, username, csrf)

    if not posts:
        print(
            "\n⚠️ 0 posts scraped."
            "\n   If you see '429 Rate-limited' above, the LINKEDIN_LI_AT cookie belongs to"
            "\n   a low-trust/new account. Update it with a cookie from an ESTABLISHED account"
            "\n   (yours or David Territo's own account) and re-run."
        )

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
