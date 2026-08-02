import json
import time
import random
import os
import base64
import re
import requests
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
    if match:
        return match.group(1).strip('/')
    return ""


# ═══════════════════════════════════════════════════════════════
# GITHUB SECRET AUTO-REFRESH
# ═══════════════════════════════════════════════════════════════

def _encrypt_secret_for_github(public_key_b64: str, secret_value: str) -> str:
    pk = nacl_public.PublicKey(public_key_b64.encode("utf-8"), encoding.Base64Encoder())
    sealed_box = nacl_public.SealedBox(pk)
    encrypted = sealed_box.encrypt(secret_value.encode("utf-8"))
    return base64.b64encode(encrypted).decode("utf-8")


def refresh_github_secret(secret_name: str, new_value: str) -> bool:
    if not GH_PAT or not GITHUB_REPOSITORY:
        print("   ⚠️  GH_PAT not set — skipping auto-refresh")
        return False
    headers = {
        "Authorization": f"Bearer {GH_PAT}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
    }
    base_url = f"https://api.github.com/repos/{GITHUB_REPOSITORY}/actions/secrets"
    key_resp  = requests.get(f"{base_url}/public-key", headers=headers, timeout=10)
    if key_resp.status_code != 200:
        return False
    key_data      = key_resp.json()
    encrypted_val = _encrypt_secret_for_github(key_data["key"], new_value)
    put_resp = requests.put(
        f"{base_url}/{secret_name}",
        headers=headers,
        json={"encrypted_value": encrypted_val, "key_id": key_data["key_id"]},
        timeout=10,
    )
    if put_resp.status_code in (201, 204):
        print(f"   ✅ Secret '{secret_name}' auto-refreshed")
        return True
    return False


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
        "name":     "li_at",
        "value":    LI_AT_COOKIE,
        "domain":   ".linkedin.com",
        "path":     "/",
        "httpOnly": True,
        "secure":   True,
        "sameSite": "None",
    }])
    print("   ✅ Session cookie injected")


def verify_session(page) -> None:
    """Load LinkedIn homepage and verify session is active."""
    print("   Verifying session …")
    try:
        page.goto("https://www.linkedin.com/", wait_until="domcontentloaded", timeout=DEFAULT_TIMEOUT)
    except Exception as e:
        if "ERR_TOO_MANY_REDIRECTS" in str(e):
            raise RuntimeError(
                "❌ Cookie is INVALID/REVOKED — use a throwaway account cookie only.\n"
                "Get a fresh li_at from a throwaway LinkedIn account and update GitHub Secrets."
            ) from None
        raise

    human_delay(3, 5)
    print_page_info(page, "session-check")
    save_debug_screenshot(page, "01_session_check")

    url = page.url
    if "login" in url or "authwall" in url:
        raise RuntimeError("❌ Cookie has EXPIRED. Get a fresh li_at and update GitHub Secrets.")

    print("   ✅ Session valid — logged in!")

    # Browse the feed naturally before navigating to a profile
    # This warms up the session and makes behaviour look more human
    print("   Warming up session (browsing feed) …")
    try:
        page.goto("https://www.linkedin.com/feed/", wait_until="domcontentloaded", timeout=30_000)
    except Exception:
        pass
    human_delay(4, 7)
    scroll_slowly(page, steps=3)
    human_delay(3, 5)


# ═══════════════════════════════════════════════════════════════
# NAVIGATION — SAME-ORIGIN FETCH STRATEGY
# ═══════════════════════════════════════════════════════════════

def fetch_activity_page_html(page, username: str) -> str:
    """
    Use LinkedIn's own fetch() from within the page context to retrieve
    the activity page HTML. Because this is a same-origin request from
    within linkedin.com, it uses the authenticated session cookies
    automatically and bypasses the navigation-level bot detection that
    blocks page.goto() calls from cloud server IPs.
    """
    activity_url = f"/in/{username}/recent-activity/all/"
    print(f"   Fetching activity page via same-origin fetch: {activity_url}")

    html = page.evaluate(f"""
        async () => {{
            try {{
                const resp = await fetch('{activity_url}', {{
                    method: 'GET',
                    credentials: 'include',
                    headers: {{
                        'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8',
                        'Accept-Language': 'en-US,en;q=0.9',
                        'Cache-Control': 'no-cache',
                    }}
                }});
                if (!resp.ok) return '';
                return await resp.text();
            }} catch(e) {{
                return '';
            }}
        }}
    """)
    return html or ""


def navigate_to_activity(page, username: str) -> bool:
    """
    Navigate to the client's LinkedIn activity page.

    Strategy A: page.goto() with networkidle (works if not blocked)
    Strategy B: Same-origin fetch() — loads HTML without triggering
                navigation-level bot detection, then sets it as page content
    Strategy C: page.goto() directly to activity URL as final fallback

    Returns True if we successfully landed on the activity page,
    False if we fell back to the profile page.
    """
    base_url     = f"https://www.linkedin.com/in/{username}/"
    activity_url = f"https://www.linkedin.com/in/{username}/recent-activity/all/"

    # ── Strategy A: Direct goto ───────────────────────────────────────────────
    print(f"\n   [Strategy A] Direct navigation to activity URL …")
    for attempt in range(2):
        try:
            page.goto(activity_url, wait_until="domcontentloaded", timeout=DEFAULT_TIMEOUT)
            human_delay(3, 5)
            url = page.url
            print_page_info(page, f"strategy-A-attempt-{attempt+1}")
            if "recent-activity" in url or username in url:
                save_debug_screenshot(page, "02_activity_page")
                print("   ✅ Strategy A succeeded!")
                return True
            print(f"   ⚠️  Redirected to {url} — retrying …")
            human_delay(4 + attempt * 3, 7 + attempt * 3)
        except Exception as e:
            print(f"   ⚠️  Strategy A attempt {attempt+1} failed: {e}")
            human_delay(3, 5)

    # ── Strategy B: Same-origin fetch (avoids navigation-level blocking) ──────
    print(f"\n   [Strategy B] Same-origin fetch of activity page …")

    # First ensure we're on a LinkedIn page so fetch() is same-origin
    try:
        if "linkedin.com" not in page.url:
            page.goto("https://www.linkedin.com/feed/", wait_until="domcontentloaded", timeout=30_000)
            human_delay(3, 5)
    except Exception:
        pass

    html = fetch_activity_page_html(page, username)

    if html and len(html) > 5000:
        print(f"   ✅ Fetched {len(html):,} bytes of HTML")
        # Inject the fetched HTML into the page so we can use Playwright selectors on it
        page.set_content(html, wait_until="domcontentloaded")
        human_delay(2, 3)
        save_debug_screenshot(page, "02_activity_fetched")
        return True
    else:
        print(f"   ⚠️  Strategy B returned only {len(html)} bytes — insufficient")

    # ── Strategy C: Navigate to base profile, click Show all posts ────────────
    print(f"\n   [Strategy C] Navigate base profile → click Show all posts …")
    try:
        page.goto(base_url, wait_until="domcontentloaded", timeout=DEFAULT_TIMEOUT)
        human_delay(4, 6)
    except Exception as e:
        print(f"   ⚠️  Base profile load: {e}")

    # Scroll to reveal Activity section
    for _ in range(5):
        page.evaluate("window.scrollBy(0, window.innerHeight * 0.8)")
        human_delay(1.5, 2.5)
    save_debug_screenshot(page, "02_profile_scrolled")

    click_selectors = [
        "a[href*='recent-activity']:has-text('Show all')",
        "a[href*='recent-activity']:has-text('See all')",
        "a[href*='recent-activity/all']",
        "a[href*='recent-activity']",
    ]
    for sel in click_selectors:
        try:
            link = page.locator(sel).first
            if link.is_visible(timeout=4_000):
                print(f"   ✅ Clicking activity link via: {sel}")
                link.click()
                page.wait_for_load_state("domcontentloaded", timeout=DEFAULT_TIMEOUT)
                human_delay(3, 5)
                save_debug_screenshot(page, "02_after_click")
                return True
        except Exception:
            continue

    print("   ⚠️  All strategies exhausted — will extract from current page")
    save_debug_screenshot(page, "02_final_state")
    return False


# ═══════════════════════════════════════════════════════════════
# POST EXTRACTION
# ═══════════════════════════════════════════════════════════════

def extract_posts(page) -> list[dict]:
    """Extract up to 6 posts from the current page."""
    posts = []

    # Activity/feed page selectors
    feed_selectors = [
        ".feed-shared-update-v2",
        ".occludable-update",
        "[data-urn*='activity']",
        ".profile-creator-shared-feed-update__container",
        ".update-components-actor",
        "[data-id*='activity']",
    ]

    # Profile page Activity section selectors
    profile_selectors = [
        ".pvs-list__item--line-separated",
        ".artdeco-list__item",
        ".pv-recent-activity-section-v2__item",
        "li.artdeco-list__item",
        ".pvs-entity",
        ".scaffold-finite-scroll__content > div",
    ]

    post_elements = []
    for group_name, group in [("feed", feed_selectors), ("profile", profile_selectors)]:
        for sel in group:
            try:
                page.wait_for_selector(sel, timeout=10_000)
                elements = page.query_selector_all(sel)
                if elements:
                    print(f"   Found {len(elements)} elements [{group_name}] via: {sel}")
                    post_elements = elements
                    break
            except PWTimeout:
                print(f"   — Not found: {sel}")
        if post_elements:
            break

    if not post_elements:
        save_debug_screenshot(page, "04_no_posts_found")

        # Last-resort: dump page HTML snippet to help diagnose selectors
        try:
            body_html = page.inner_html("body")
            with open("page_body_snippet.txt", "w", encoding="utf-8") as f:
                f.write(body_html[:20_000])
            print("   📄 Saved first 20KB of page HTML → page_body_snippet.txt")
        except Exception:
            pass

        print("   ⚠️  No post elements found — see 04_no_posts_found.png + page_body_snippet.txt")
        return posts

    for el in post_elements[:14]:
        try:
            # Skip sponsored
            sponsored = el.query_selector(".feed-shared-actor__sub-description")
            if sponsored and "promoted" in (sponsored.inner_text() or "").lower():
                continue

            text_el = (
                el.query_selector(".feed-shared-update-v2__description")
                or el.query_selector(".feed-shared-text")
                or el.query_selector(".break-words")
                or el.query_selector("[data-test-id='main-feed-activity-card__commentary']")
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
        raise ValueError("LINKEDIN_LI_AT secret is not set.")
    if not LINKEDIN_PROFILE_URL:
        raise ValueError("LINKEDIN_PROFILE_URL secret is not set.")

    username = extract_username_from_url(LINKEDIN_PROFILE_URL)
    if not username:
        raise ValueError(f"Cannot extract username from: {LINKEDIN_PROFILE_URL}")

    print("🚀 Starting LinkedIn scraper …")
    print(f"   Client username: {username}")

    with sync_playwright() as p:
        browser = p.chromium.launch(
            headless=True,
            args=[
                "--no-sandbox",
                "--disable-setuid-sandbox",
                "--disable-blink-features=AutomationControlled",
                "--disable-infobars",
                "--disable-extensions",
                "--disable-dev-shm-usage",
                "--window-size=1366,768",
            ],
        )

        context = browser.new_context(
            user_agent=(
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/125.0.0.0 Safari/537.36"
            ),
            viewport={"width": 1366, "height": 768},
            locale="en-US",
            timezone_id="America/New_York",
            java_script_enabled=True,
        )
        context.set_default_timeout(DEFAULT_TIMEOUT)

        inject_session_cookie(context)
        page = context.new_page()
        stealth_sync(page)

        posts = []
        try:
            # 1. Verify session + warm up on feed
            verify_session(page)

            # 2. Navigate to activity page (3 strategies)
            navigate_to_activity(page, username)

            # 3. Scroll to load posts
            print("   Scrolling to load posts …")
            scroll_slowly(page, steps=5)
            save_debug_screenshot(page, "03_after_scroll")

            # 4. Extract posts
            print("   Extracting posts …")
            posts = extract_posts(page)

            # 5. Auto-refresh cookie
            print("\n   Checking for refreshed cookie …")
            fresh = extract_fresh_li_at(context)
            if fresh and fresh != LI_AT_COOKIE:
                print("   🔄 Cookie refreshed — saving …")
                refresh_github_secret("LINKEDIN_LI_AT", fresh)
            elif fresh:
                print("   ✅ Cookie unchanged — still fresh")

        except Exception as e:
            save_debug_screenshot(page, "error_state")
            browser.close()
            raise e

        browser.close()

    output = {
        "updated_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "post_count": len(posts),
        "posts":      posts,
    }
    with open("posts.json", "w", encoding="utf-8") as f:
        json.dump(output, f, ensure_ascii=False, indent=2)

    print(f"\n✅ Done! Scraped {len(posts)} posts → posts.json")
    if len(posts) == 0:
        print("⚠️  0 posts — check screenshots + page_body_snippet.txt in artifacts.")


if __name__ == "__main__":
    scrape()
