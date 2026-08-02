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

DEFAULT_TIMEOUT = 60_000  # ms


# ═══════════════════════════════════════════════════════════════
# UTILITIES
# ═══════════════════════════════════════════════════════════════

def human_delay(min_s: float = 2.0, max_s: float = 5.0) -> None:
    time.sleep(random.uniform(min_s, max_s))


def scroll_slowly(page, steps: int = 5) -> None:
    for _ in range(steps):
        page.evaluate("window.scrollBy(0, window.innerHeight * 0.75)")
        human_delay(1.5, 3.0)


def save_debug_screenshot(page, name: str) -> None:
    try:
        page.screenshot(path=f"{name}.png", full_page=True)
        print(f"   📸 {name}.png")
    except Exception as e:
        print(f"   ⚠️  Screenshot failed: {e}")


def print_page_info(page, label: str = "") -> None:
    try:
        print(f"   [{label}] URL:   {page.url}")
        print(f"   [{label}] Title: {page.title()}")
    except Exception:
        pass


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
    ok = resp.status_code in (201, 204)
    if ok:
        print(f"   ✅ Secret '{secret_name}' auto-refreshed")
    return ok


def extract_fresh_li_at(context) -> str:
    for c in context.cookies():
        if c["name"] == "li_at" and "linkedin.com" in c.get("domain", ""):
            return c["value"]
    return ""


# ═══════════════════════════════════════════════════════════════
# SESSION
# ═══════════════════════════════════════════════════════════════

def inject_session_cookie(context) -> None:
    context.add_cookies([{
        "name": "li_at", "value": LI_AT_COOKIE,
        "domain": ".linkedin.com", "path": "/",
        "httpOnly": True, "secure": True, "sameSite": "None",
    }])
    print("   ✅ Session cookie injected")


def verify_session(page) -> None:
    """Verify session using the homepage only — does NOT browse the feed.
    We deliberately avoid loading the LinkedIn SPA/React app so its router
    cannot intercept subsequent profile navigations."""
    print("   Verifying session …")
    try:
        page.goto("https://www.linkedin.com/", wait_until="domcontentloaded", timeout=DEFAULT_TIMEOUT)
    except Exception as e:
        if "ERR_TOO_MANY_REDIRECTS" in str(e):
            raise RuntimeError("❌ Cookie INVALID/REVOKED — refresh li_at in GitHub Secrets.") from None
        raise

    human_delay(2, 4)
    print_page_info(page, "session-check")
    save_debug_screenshot(page, "01_session_check")

    url = page.url
    if "login" in url or "authwall" in url:
        raise RuntimeError("❌ Cookie EXPIRED — refresh li_at in GitHub Secrets.")
    print("   ✅ Session valid!")

    # CRITICAL: Navigate to about:blank to completely unload the LinkedIn SPA.
    # The React router on linkedin.com/feed intercepts ALL profile URL navigations
    # and redirects them back to /feed, causing ERR_TOO_MANY_REDIRECTS.
    # Going to about:blank destroys the SPA context so the next navigation is clean.
    print("   Clearing SPA context (about:blank) …")
    page.goto("about:blank", wait_until="commit")
    human_delay(1, 2)


# ═══════════════════════════════════════════════════════════════
# STRATEGY 1: PLAIN REQUESTS (no browser, no SPA, no routing)
# ═══════════════════════════════════════════════════════════════

def fetch_posts_via_requests(username: str) -> list[dict]:
    """
    Fetch the activity page using Python's requests library — no browser, no Playwright.
    This completely bypasses the SPA router and Playwright navigation issues.
    LinkedIn's server-rendered HTML contains structured post data in embedded <code> tags.
    """
    url = f"https://www.linkedin.com/in/{username}/recent-activity/all/"
    print(f"   [Requests] GET {url}")

    session = req_lib.Session()
    headers = {
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36"
        ),
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
        "Accept-Language": "en-US,en;q=0.9",
        "Accept-Encoding": "gzip, deflate, br",
        "Cache-Control": "no-cache",
        "Sec-Fetch-Dest": "document",
        "Sec-Fetch-Mode": "navigate",
        "Sec-Fetch-Site": "none",
        "Sec-Fetch-User": "?1",
        "Upgrade-Insecure-Requests": "1",
        "Cookie": f"li_at={LI_AT_COOKIE}; lang=v=2&lang=en-us;",
    }

    try:
        resp = session.get(url, headers=headers, allow_redirects=True, timeout=30)
        print(f"   [Requests] Status: {resp.status_code}  Final URL: {resp.url}")
        print(f"   [Requests] Response size: {len(resp.content):,} bytes")

        # Save HTML for inspection
        with open("activity_page.html", "w", encoding="utf-8", errors="replace") as f:
            f.write(resp.text[:60_000])
        print("   📄 Saved activity_page.html (first 60KB)")

        if resp.status_code != 200:
            print(f"   ⚠️  Non-200 response from requests")
            return []

        if "login" in resp.url or "authwall" in resp.url:
            print("   ⚠️  Requests was redirected to login — cookie rejected by server")
            return []

        return parse_posts_from_html(resp.text, username)

    except Exception as e:
        print(f"   ❌ Requests error: {e}")
        return []


def parse_posts_from_html(html: str, username: str) -> list[dict]:
    """
    Parse posts from LinkedIn's server-rendered HTML.
    LinkedIn embeds page data as JSON in <code> tags with specific IDs.
    """
    posts = []

    # Pattern 1: <code> tags with JSON blobs (LinkedIn's normalized data format)
    code_tags = re.findall(r'<code[^>]*>(.*?)</code>', html, re.DOTALL)
    print(f"   Found {len(code_tags)} <code> tags in HTML")

    for raw in code_tags:
        raw = raw.strip()
        if not raw.startswith('{'):
            continue
        try:
            data = json.loads(raw)
            found = _extract_posts_from_json_blob(data, username)
            posts.extend(found)
            if len(posts) >= 6:
                break
        except (json.JSONDecodeError, Exception):
            continue

    if posts:
        print(f"   ✅ Extracted {len(posts)} posts from embedded JSON")
        return posts[:6]

    # Pattern 2: look for plain text content in HTML using regex
    # LinkedIn activity pages may have post text in specific patterns
    text_patterns = [
        r'<span[^>]*class="[^"]*break-words[^"]*"[^>]*>(.*?)</span>',
        r'"commentary":\{"text":\{"text":"([^"]{20,})"',
        r'"description":\{"text":"([^"]{20,})"',
    ]
    for pattern in text_patterns:
        matches = re.findall(pattern, html, re.DOTALL)
        for m in matches:
            text = re.sub(r'<[^>]+>', '', m).strip()
            if len(text) > 30:
                posts.append({
                    "text": text,
                    "date": "",
                    "image_url": "",
                    "post_url": f"https://www.linkedin.com/in/{username}/recent-activity/all/",
                })
            if len(posts) >= 6:
                break
        if posts:
            break

    if posts:
        print(f"   ✅ Extracted {len(posts)} posts via regex patterns")
    else:
        print("   ⚠️  Could not extract posts from HTML — see activity_page.html")

    return posts[:6]


def _extract_posts_from_json_blob(data: dict, username: str) -> list[dict]:
    """Dig into LinkedIn's normalized JSON blob to find post text."""
    posts = []
    if not isinstance(data, dict):
        return posts

    # Try 'included' array (LinkedIn normalized format)
    for item in data.get("included", []):
        if not isinstance(item, dict):
            continue

        # Get text content
        text = ""
        for key in ("commentary", "description", "text"):
            val = item.get(key, {})
            if isinstance(val, dict):
                text = val.get("text", "")
                if isinstance(text, dict):
                    text = text.get("text", "")
            elif isinstance(val, str):
                text = val
            if text and len(text) > 20:
                break

        if not text or len(text) < 20:
            continue

        # Get date
        date = ""
        for key in ("publishedAt", "createdAt", "postedAt"):
            if key in item:
                ts = item[key]
                if isinstance(ts, (int, float)):
                    date = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(ts / 1000))
                else:
                    date = str(ts)
                break

        posts.append({
            "text": text.strip(),
            "date": date,
            "image_url": "",
            "post_url": f"https://www.linkedin.com/in/{username}/recent-activity/all/",
        })

        if len(posts) >= 6:
            break

    return posts


# ═══════════════════════════════════════════════════════════════
# STRATEGY 2: PLAYWRIGHT with SPA-cleared context
# ═══════════════════════════════════════════════════════════════

def navigate_and_scrape_with_playwright(page, username: str) -> list[dict]:
    """
    Navigate to the activity page AFTER clearing the SPA context via about:blank.
    Without the React router running, page.goto() to a profile URL
    should succeed as a clean navigation.
    """
    activity_url = f"https://www.linkedin.com/in/{username}/recent-activity/all/"
    print(f"   [Playwright] Navigating to: {activity_url}")

    for attempt in range(3):
        try:
            page.goto(activity_url, wait_until="domcontentloaded", timeout=DEFAULT_TIMEOUT)
            human_delay(3, 5)
            url = page.url
            print_page_info(page, f"pw-attempt-{attempt+1}")
            save_debug_screenshot(page, f"02_playwright_attempt_{attempt+1}")

            if "recent-activity" in url or username in url:
                print(f"   ✅ Playwright navigation succeeded!")
                scroll_slowly(page, steps=5)
                save_debug_screenshot(page, "03_after_scroll")
                return extract_posts_playwright(page, username)

            print(f"   ⚠️  Redirected to {url}")
            # Clear SPA again before retry
            page.goto("about:blank", wait_until="commit")
            human_delay(3 + attempt * 2, 5 + attempt * 3)

        except Exception as e:
            print(f"   ⚠️  Playwright attempt {attempt+1} failed: {e}")
            try:
                page.goto("about:blank", wait_until="commit")
            except Exception:
                pass
            human_delay(3 + attempt * 2, 5 + attempt * 3)

    print("   ⚠️  All Playwright attempts exhausted")
    return []


def extract_posts_playwright(page, username: str) -> list[dict]:
    """Extract posts from the loaded Playwright page."""
    posts = []
    selectors = [
        ".feed-shared-update-v2",
        ".occludable-update",
        "[data-urn*='activity']",
        ".profile-creator-shared-feed-update__container",
        ".update-components-actor",
    ]

    post_elements = []
    for sel in selectors:
        try:
            page.wait_for_selector(sel, timeout=10_000)
            elements = page.query_selector_all(sel)
            if elements:
                print(f"   Found {len(elements)} elements via: {sel}")
                post_elements = elements
                break
        except PWTimeout:
            print(f"   — Not found: {sel}")

    if not post_elements:
        # Save HTML for diagnosis
        try:
            body = page.inner_html("body")
            with open("page_body.txt", "w", encoding="utf-8") as f:
                f.write(body[:30_000])
            print("   📄 Saved page_body.txt")
        except Exception:
            pass
        save_debug_screenshot(page, "04_no_posts_found")
        return posts

    for el in post_elements[:14]:
        try:
            sponsored = el.query_selector(".feed-shared-actor__sub-description")
            if sponsored and "promoted" in (sponsored.inner_text() or "").lower():
                continue

            text_el = (
                el.query_selector(".feed-shared-update-v2__description")
                or el.query_selector(".feed-shared-text")
                or el.query_selector(".break-words")
                or el.query_selector(".update-components-text")
            )
            raw_text = text_el.inner_text().strip() if text_el else ""
            if not raw_text:
                continue

            time_el  = el.query_selector("time")
            image_el = (
                el.query_selector(".feed-shared-image__image")
                or el.query_selector(".update-components-image__image")
                or el.query_selector("img.ivm-view-attr__img--centered")
            )
            link_el = (
                el.query_selector("a[href*='/feed/update/']")
                or el.query_selector("a.app-aware-link[href*='activity']")
            )

            posts.append({
                "text":      raw_text,
                "date":      time_el.get_attribute("datetime") if time_el else "",
                "image_url": image_el.get_attribute("src") if image_el else "",
                "post_url":  link_el.get_attribute("href") if link_el else LINKEDIN_PROFILE_URL,
            })
            if len(posts) == 6:
                break

        except Exception as e:
            print(f"   ⚠️  Skipping post: {e}")

    return posts


# ═══════════════════════════════════════════════════════════════
# MAIN
# ═══════════════════════════════════════════════════════════════

def scrape() -> None:
    if not LI_AT_COOKIE:
        raise ValueError("LINKEDIN_LI_AT secret not set.")
    if not LINKEDIN_PROFILE_URL:
        raise ValueError("LINKEDIN_PROFILE_URL secret not set.")

    username = extract_username_from_url(LINKEDIN_PROFILE_URL)
    if not username:
        raise ValueError(f"Cannot extract username from: {LINKEDIN_PROFILE_URL}")

    print("🚀 Starting LinkedIn scraper …")
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
            # 1. Verify session + unload SPA (goes to about:blank at the end)
            verify_session(page)

            # 2. Strategy 1: Plain requests (no browser, no SPA router)
            print("\n── Strategy 1: Plain requests library ──────────────────────")
            posts = fetch_posts_via_requests(username)

            # 3. Strategy 2: Playwright with clean (SPA-free) context
            if not posts:
                print("\n── Strategy 2: Playwright (SPA-cleared context) ────────────")
                posts = navigate_and_scrape_with_playwright(page, username)

            # 4. Auto-refresh cookie
            if posts:
                print("\n   Checking for refreshed cookie …")
                fresh = extract_fresh_li_at(context)
                if fresh and fresh != LI_AT_COOKIE:
                    refresh_github_secret("LINKEDIN_LI_AT", fresh)
                elif fresh:
                    print("   ✅ Cookie unchanged — still fresh")

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
            "\n⚠️  0 posts scraped. Check artifacts:\n"
            "  • activity_page.html — what LinkedIn's server actually returned\n"
            "  • page_body.txt — what the Playwright page contained\n"
            "  • *.png — visual screenshots at each step\n"
        )


if __name__ == "__main__":
    scrape()
