import json
import time
import random
import os
import base64
import re
import requests as req_lib
from nacl import encoding, public as nacl_public

from playwright.sync_api import sync_playwright, TimeoutError as PWTimeout
from playwright_stealth import stealth_sync

# ─── CONFIGURATION ─────────────────────────────────────────────────────────────
LI_AT_COOKIE         = os.environ.get("LINKEDIN_LI_AT", "")
LINKEDIN_PROFILE_URL = os.environ.get("LINKEDIN_PROFILE_URL", "")
GH_PAT               = os.environ.get("GH_PAT", "")
GITHUB_REPOSITORY    = os.environ.get("GITHUB_REPOSITORY", "")
# ──────────────────────────────────────────────────────────────────────────────

DEFAULT_TIMEOUT = 60_000


# ═══════════════════════════════════════════════════════════════
# UTILITIES
# ═══════════════════════════════════════════════════════════════

def human_delay(min_s=2.0, max_s=5.0):
    time.sleep(random.uniform(min_s, max_s))


def save_debug_screenshot(page, name):
    try:
        page.screenshot(path=f"{name}.png", full_page=True)
        print(f"   📸 {name}.png")
    except Exception as e:
        print(f"   ⚠️  Screenshot failed: {e}")


def print_page_info(page, label=""):
    try:
        print(f"   [{label}] URL:   {page.url}")
        print(f"   [{label}] Title: {page.title()}")
    except Exception:
        pass


def extract_username_from_url(url):
    match = re.search(r'linkedin\.com/in/([^/?#]+)', url)
    return match.group(1).strip('/') if match else ""


# ═══════════════════════════════════════════════════════════════
# GITHUB SECRET AUTO-REFRESH
# ═══════════════════════════════════════════════════════════════

def _encrypt_secret(pub_key_b64, value):
    pk  = nacl_public.PublicKey(pub_key_b64.encode(), encoding.Base64Encoder())
    box = nacl_public.SealedBox(pk)
    return base64.b64encode(box.encrypt(value.encode())).decode()


def refresh_github_secret(secret_name, new_value):
    if not GH_PAT or not GITHUB_REPOSITORY:
        return False
    headers = {
        "Authorization": f"Bearer {GH_PAT}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
    }
    base = f"https://api.github.com/repos/{GITHUB_REPOSITORY}/actions/secrets"
    kr   = req_lib.get(f"{base}/public-key", headers=headers, timeout=10)
    if kr.status_code != 200:
        return False
    kd   = kr.json()
    resp = req_lib.put(
        f"{base}/{secret_name}",
        headers=headers,
        json={"encrypted_value": _encrypt_secret(kd["key"], new_value), "key_id": kd["key_id"]},
        timeout=10,
    )
    if resp.status_code in (201, 204):
        print(f"   ✅ Secret '{secret_name}' auto-refreshed")
        return True
    return False


def extract_fresh_li_at(context):
    for c in context.cookies():
        if c["name"] == "li_at" and "linkedin.com" in c.get("domain", ""):
            return c["value"]
    return ""


# ═══════════════════════════════════════════════════════════════
# SESSION
# ═══════════════════════════════════════════════════════════════

def inject_session_cookie(context):
    context.add_cookies([{
        "name": "li_at", "value": LI_AT_COOKIE,
        "domain": ".linkedin.com", "path": "/",
        "httpOnly": True, "secure": True, "sameSite": "None",
    }])
    print("   ✅ Session cookie injected")


def load_linkedin_feed(page):
    """
    Load the LinkedIn feed page and verify the session is active.
    We stay on the feed page (NOT about:blank) so we can make
    Voyager API calls from within the authenticated LinkedIn context.
    """
    print("   Loading LinkedIn feed …")
    try:
        page.goto("https://www.linkedin.com/feed/", wait_until="domcontentloaded", timeout=DEFAULT_TIMEOUT)
    except Exception as e:
        if "ERR_TOO_MANY_REDIRECTS" in str(e):
            raise RuntimeError("❌ Cookie INVALID/REVOKED — get a fresh li_at from your throwaway account.") from None
        raise

    human_delay(4, 7)
    print_page_info(page, "feed")
    save_debug_screenshot(page, "01_feed")

    url = page.url
    if "login" in url or "authwall" in url:
        raise RuntimeError("❌ Cookie EXPIRED — get a fresh li_at from your throwaway account.")

    print("   ✅ Session valid — on LinkedIn feed!")


# ═══════════════════════════════════════════════════════════════
# VOYAGER API (LinkedIn's internal REST API)
# ═══════════════════════════════════════════════════════════════

VOYAGER_JS = """
async (username) => {
    // Extract CSRF token from JSESSIONID cookie
    const getCsrf = () => {
        const m = document.cookie.match(/JSESSIONID=([^;]+)/);
        if (!m) return '';
        return m[1].replace(/^"/, '').replace(/"$/, '');
    };

    const csrf = getCsrf();
    const headers = {
        'accept': 'application/vnd.linkedin.normalized+json+2.1',
        'csrf-token': csrf,
        'x-li-lang': 'en_US',
        'x-li-page-instance': 'urn:li:page:d_flagship3_profile_view_base;',
        'x-restli-protocol-version': '2.0.0',
        'x-li-track': '{"clientVersion":"1.13.10516","mpVersion":"1.13.10516","osName":"web","timezoneOffset":0,"timezone":"America/New_York","deviceFormFactor":"DESKTOP","mpName":"voyager-web"}',
    };

    const log = [];

    try {
        // ── Step 1: Resolve vanity name → entity URN ─────────────────────
        const profileUrl = `/voyager/api/identity/profiles/${username}?projection=(id,entityUrn,miniProfile)`;
        const profResp = await fetch(profileUrl, { headers, credentials: 'include' });
        log.push(`Profile fetch: ${profResp.status}`);

        if (!profResp.ok) {
            const text = await profResp.text();
            return { error: 'profile_fetch_failed', status: profResp.status, log, body: text.substring(0, 500) };
        }

        const profData = await profResp.json();
        log.push(`Profile data keys: ${Object.keys(profData).join(', ')}`);

        // LinkedIn normalizes responses — entityUrn is at profData.data.entityUrn
        const entityUrn = (profData.data && profData.data.entityUrn)
            || profData.entityUrn
            || '';

        log.push(`Entity URN: ${entityUrn}`);

        if (!entityUrn) {
            return {
                error: 'no_entity_urn',
                log,
                sampleData: JSON.stringify(profData).substring(0, 1000)
            };
        }

        // ── Step 2: Fetch recent activity posts ──────────────────────────
        const encodedUrn = encodeURIComponent(entityUrn);
        const postsUrl = `/voyager/api/feed/updates?profileId=${encodedUrn}&count=15&moduleKey=memberFeedModule&includeLongTermHistory=true`;
        const postsResp = await fetch(postsUrl, { headers, credentials: 'include' });
        log.push(`Posts fetch: ${postsResp.status}`);

        if (!postsResp.ok) {
            const text = await postsResp.text();
            return { error: 'posts_fetch_failed', status: postsResp.status, log, body: text.substring(0, 500) };
        }

        const postsData = await postsResp.json();
        log.push(`Posts response keys: ${Object.keys(postsData).join(', ')}`);
        log.push(`Included items: ${(postsData.included || []).length}`);
        log.push(`Elements: ${(postsData.data && postsData.data.elements ? postsData.data.elements.length : 0)}`);

        return { success: true, entityUrn, log, data: postsData };

    } catch(e) {
        return { error: e.toString(), log };
    }
}
"""


def call_voyager_api(page, username):
    """Call LinkedIn's Voyager API from within the authenticated feed page."""
    print(f"   Calling Voyager API for: {username}")
    result = page.evaluate(VOYAGER_JS, username)
    return result


def parse_voyager_posts(api_result, username):
    """Parse posts from Voyager API response."""
    posts = []

    if not api_result or not isinstance(api_result, dict):
        return posts

    data = api_result.get("data", {})
    included = api_result.get("included", [])

    # The normalized JSON format puts all data in 'included'
    # Each item has a '$type' field indicating what it is
    for item in included:
        if not isinstance(item, dict):
            continue

        item_type = item.get("$type", "")

        # Look for feed update items with text content
        text = ""

        # Pattern 1: commentary object
        commentary = item.get("commentary", {})
        if isinstance(commentary, dict):
            text_obj = commentary.get("text", {})
            if isinstance(text_obj, dict):
                text = text_obj.get("text", "")
            elif isinstance(text_obj, str):
                text = text_obj

        # Pattern 2: description object (older format)
        if not text:
            desc = item.get("description", {})
            if isinstance(desc, dict):
                text = desc.get("text", "")

        # Pattern 3: direct text field
        if not text:
            text_field = item.get("text", {})
            if isinstance(text_field, dict):
                text = text_field.get("text", "")
            elif isinstance(text_field, str) and len(text_field) > 30:
                text = text_field

        if not text or len(text.strip()) < 20:
            continue

        # Get date
        date = ""
        for key in ("publishedAt", "createdAt", "postedAt", "updatedAt"):
            if key in item:
                ts = item[key]
                if isinstance(ts, (int, float)) and ts > 0:
                    date = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(ts / 1000))
                elif isinstance(ts, str):
                    date = ts
                break

        # Get image
        image_url = ""
        images = item.get("content", {})
        if isinstance(images, dict):
            root_url = images.get("url", "") or images.get("rootUrl", "")
            if root_url:
                image_url = root_url

        # Get post URL
        post_url = LINKEDIN_PROFILE_URL
        actor_urn = item.get("actor", "") or item.get("socialDetail", {})
        if isinstance(actor_urn, dict):
            actor_urn = actor_urn.get("urn", "")

        posts.append({
            "text":      text.strip(),
            "date":      date,
            "image_url": image_url,
            "post_url":  post_url,
        })

        if len(posts) == 6:
            break

    return posts


# ═══════════════════════════════════════════════════════════════
# MAIN
# ═══════════════════════════════════════════════════════════════

def scrape():
    if not LI_AT_COOKIE:
        raise ValueError("LINKEDIN_LI_AT secret not set.")
    if not LINKEDIN_PROFILE_URL:
        raise ValueError("LINKEDIN_PROFILE_URL secret not set.")

    username = extract_username_from_url(LINKEDIN_PROFILE_URL)
    if not username:
        raise ValueError(f"Cannot extract username from: {LINKEDIN_PROFILE_URL}")

    print("🚀 Starting LinkedIn scraper (Voyager API) …")
    print(f"   Client username: {username}")

    with sync_playwright() as p:
        browser = p.chromium.launch(
            headless=True,
            args=[
                "--no-sandbox", "--disable-setuid-sandbox",
                "--disable-blink-features=AutomationControlled",
                "--disable-dev-shm-usage", "--window-size=1366,768",
            ],
        )
        context = browser.new_context(
            user_agent=(
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36"
            ),
            viewport={"width": 1366, "height": 768},
            locale="en-US", timezone_id="America/New_York",
        )
        context.set_default_timeout(DEFAULT_TIMEOUT)
        inject_session_cookie(context)

        page = context.new_page()
        stealth_sync(page)

        posts = []
        try:
            # 1. Load feed (establishes authenticated context with JSESSIONID)
            load_linkedin_feed(page)

            # 2. Call Voyager API from within the LinkedIn page context
            print("\n── Voyager API ─────────────────────────────────────────────")
            api_result = call_voyager_api(page, username)

            # Log diagnostic info
            print(f"   API call log:")
            for line in api_result.get("log", []):
                print(f"     {line}")

            if api_result.get("error"):
                print(f"   ❌ API error: {api_result['error']}")
                if "sampleData" in api_result:
                    print(f"   Sample data: {api_result['sampleData'][:500]}")
                if "body" in api_result:
                    print(f"   Response body: {api_result['body']}")
            elif api_result.get("success"):
                entity_urn = api_result.get("entityUrn", "")
                print(f"   ✅ API succeeded! Entity URN: {entity_urn}")

                # Save raw API response for reference
                raw_data = api_result.get("data", {})
                with open("voyager_response.json", "w", encoding="utf-8") as f:
                    json.dump(raw_data, f, ensure_ascii=False, indent=2)
                print("   📄 Saved voyager_response.json")

                # Parse posts from response
                posts = parse_voyager_posts(raw_data, username)
                print(f"   Extracted {len(posts)} posts")

                if not posts:
                    # Try parsing from the full result (included may be top-level)
                    posts = parse_voyager_posts(api_result, username)
                    print(f"   Re-tried from full result: {len(posts)} posts")

            # 3. Auto-refresh cookie
            fresh = extract_fresh_li_at(context)
            if fresh and fresh != LI_AT_COOKIE:
                print("   🔄 Cookie refreshed by LinkedIn — saving …")
                refresh_github_secret("LINKEDIN_LI_AT", fresh)
            elif fresh:
                print("   ✅ Cookie unchanged — still fresh")

            save_debug_screenshot(page, "02_final_state")

        except Exception as e:
            try:
                save_debug_screenshot(page, "error_state")
            except Exception:
                pass
            browser.close()
            raise e

        browser.close()

    output = {
        "updated_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "post_count": len(posts),
        "posts": posts,
    }
    with open("posts.json", "w", encoding="utf-8") as f:
        json.dump(output, f, ensure_ascii=False, indent=2)

    print(f"\n✅ Done! Scraped {len(posts)} posts → posts.json")
    if len(posts) == 0:
        print(
            "\n⚠️  0 posts. Key debug files in artifacts:\n"
            "  • voyager_response.json — raw API response (if API was reached)\n"
            "  • *.png screenshots\n"
        )


if __name__ == "__main__":
    scrape()
