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


def extract_fresh_li_at(context) -> str:
    for c in context.cookies():
        if c["name"] == "li_at" and "linkedin.com" in c.get("domain", ""):
            return c["value"]
    return ""


# ═══════════════════════════════════════════════════════════════
# SESSION SETUP
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


def verify_and_warmup_session(page) -> None:
    print("   Verifying session on feed page …")
    try:
        page.goto("https://www.linkedin.com/feed/", wait_until="domcontentloaded", timeout=DEFAULT_TIMEOUT)
    except Exception as e:
        if "ERR_TOO_MANY_REDIRECTS" in str(e):
            raise RuntimeError("❌ Cookie is INVALID or REVOKED. Update LINKEDIN_LI_AT in GitHub Secrets.") from None
        raise

    human_delay(3, 5)
    print_page_info(page, "feed-check")
    save_debug_screenshot(page, "01_feed_check")

    url = page.url
    if "login" in url or "authwall" in url or "signup" in url:
        raise RuntimeError("❌ Cookie has EXPIRED. Get a fresh li_at cookie and update GitHub Secrets.")

    print("   ✅ Session valid — logged in!")


# ═══════════════════════════════════════════════════════════════
# NETWORK RESPONSE INTERCEPTOR
# ═══════════════════════════════════════════════════════════════

class NetworkPostTracker:
    def __init__(self, username: str):
        self.username = username
        self.captured_posts = []

    def handle_response(self, response):
        url = response.url
        if not ("voyager/api" in url or "graphql" in url or "feed/updates" in url or "identity/profiles" in url or "dash/channels" in url):
            return

        if response.status != 200:
            return

        try:
            content_type = response.headers.get("content-type", "")
            if "json" not in content_type:
                return

            data = response.json()
            extracted = self._parse_json_payload(data)
            for p in extracted:
                if not any(existing["text"] == p["text"] for existing in self.captured_posts):
                    self.captured_posts.append(p)
                    print(f"   📡 [Network Interceptor] Captured post ({len(p['text'])} chars): {p['text'][:60]}...")
        except Exception:
            pass

    def _parse_json_payload(self, data) -> list[dict]:
        posts = []
        if not isinstance(data, dict):
            return posts

        included = data.get("included", [])
        if isinstance(included, list):
            for item in included:
                if not isinstance(item, dict):
                    continue
                text = ""
                # Check commentary or description
                commentary = item.get("commentary", {})
                if isinstance(commentary, dict):
                    t_obj = commentary.get("text", {})
                    if isinstance(t_obj, dict):
                        text = t_obj.get("text", "")
                    elif isinstance(t_obj, str):
                        text = t_obj

                if not text:
                    desc = item.get("description", {})
                    if isinstance(desc, dict):
                        text = desc.get("text", "")

                if not text and isinstance(item.get("text"), str) and len(item["text"]) > 25:
                    text = item["text"]

                if text and len(text.strip()) > 20:
                    date = ""
                    for k in ("publishedAt", "createdAt", "postedAt"):
                        if k in item and isinstance(item[k], (int, float)):
                            date = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(item[k] / 1000))
                            break

                    image_url = ""
                    content = item.get("content", {})
                    if isinstance(content, dict):
                        image_url = content.get("url", "") or content.get("rootUrl", "")

                    posts.append({
                        "text":      text.strip(),
                        "date":      date,
                        "image_url": image_url,
                        "post_url":  f"https://www.linkedin.com/in/{self.username}/recent-activity/all/",
                    })

        return posts


# ═══════════════════════════════════════════════════════════════
# ROBUST UI & SOFT NAVIGATION TO PROFILE / ACTIVITY
# ═══════════════════════════════════════════════════════════════

def navigate_to_user_activity(page, username: str) -> None:
    """
    1. Load base profile first: https://www.linkedin.com/in/{username}/
    2. Soft-click or soft-navigate to activity page to avoid ERR_ABORTED
    """
    base_profile_url = f"https://www.linkedin.com/in/{username}/"
    activity_url = f"https://www.linkedin.com/in/{username}/recent-activity/all/"

    print(f"   Step 1: Loading base profile: {base_profile_url}")
    try:
        page.goto(base_profile_url, wait_until="domcontentloaded", timeout=DEFAULT_TIMEOUT)
        human_delay(3, 5)
        print_page_info(page, "base-profile")
        save_debug_screenshot(page, "02a_base_profile")
    except Exception as e:
        print(f"   ⚠️ Base profile load notice: {e}")

    # Scroll profile to load Activity section
    print("   Scrolling base profile ...")
    for _ in range(4):
        page.evaluate("window.scrollBy(0, window.innerHeight * 0.75)")
        human_delay(1.5, 2.5)

    save_debug_screenshot(page, "02b_profile_scrolled")

    # Step 2: Try clicking 'Show all posts' button on profile page
    show_all_selectors = [
        "a[href*='recent-activity']:has-text('Show all')",
        "a[href*='recent-activity']:has-text('See all')",
        "a[href*='recent-activity/all']",
        "a[href*='recent-activity']",
        ".pv-recent-activity-section a",
        "button:has-text('Show all posts')",
        "span:has-text('Show all posts')",
    ]

    clicked = False
    for sel in show_all_selectors:
        try:
            link = page.locator(sel).first
            if link.is_visible(timeout=3_000):
                print(f"   ✅ Found & clicking Activity link: {sel}")
                link.click()
                human_delay(4, 6)
                save_debug_screenshot(page, "02c_after_link_click")
                clicked = True
                break
        except Exception:
            continue

    if not clicked:
        print("   Step 3: Triggering soft JS location change to activity feed ...")
        try:
            page.evaluate(f"window.location.href = '{activity_url}'")
            human_delay(4, 6)
            save_debug_screenshot(page, "02c_soft_js_nav")
        except Exception as e:
            print(f"   ⚠️ Soft JS nav notice: {e}")

    # Scroll Activity page to trigger lazy loading of post cards
    print("   Scrolling activity feed ...")
    for _ in range(6):
        page.evaluate("window.scrollBy(0, window.innerHeight * 0.75)")
        human_delay(2.0, 3.0)

    save_debug_screenshot(page, "03_after_scroll")


# ═══════════════════════════════════════════════════════════════
# DOM EXTRACTION FALLBACK
# ═══════════════════════════════════════════════════════════════

def extract_posts_from_dom(page, username: str) -> list[dict]:
    posts = []
    selectors = [
        ".feed-shared-update-v2",
        ".occludable-update",
        "[data-urn*='activity']",
        ".profile-creator-shared-feed-update__container",
        ".pvs-list__item--line-separated",
        ".artdeco-list__item",
        "li.artdeco-list__item",
        ".update-components-text",
        ".pv-recent-activity-detail-v2",
        ".pvs-entity",
    ]

    post_elements = []
    for sel in selectors:
        try:
            elements = page.query_selector_all(sel)
            if elements:
                print(f"   Found {len(elements)} DOM elements via selector: {sel}")
                post_elements = elements
                break
        except Exception:
            pass

    for el in post_elements[:14]:
        try:
            text_el = (
                el.query_selector(".feed-shared-update-v2__description")
                or el.query_selector(".feed-shared-text")
                or el.query_selector(".break-words")
                or el.query_selector(".update-components-text")
                or el.query_selector("[data-test-id='main-feed-activity-card__commentary']")
                or el.query_selector("span[dir='ltr']")
            )
            raw_text = text_el.inner_text().strip() if text_el else ""
            if not raw_text or len(raw_text) < 15:
                continue

            time_el = el.query_selector("time")
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
                "post_url":  link_el.get_attribute("href") if link_el else f"https://www.linkedin.com/in/{username}/recent-activity/all/",
            })
            if len(posts) == 6:
                break
        except Exception:
            continue

    return posts


# ═══════════════════════════════════════════════════════════════
# MAIN SCRAPE EXECUTION
# ═══════════════════════════════════════════════════════════════

def scrape() -> None:
    if not LI_AT_COOKIE:
        raise ValueError("LINKEDIN_LI_AT secret is not set.")
    if not LINKEDIN_PROFILE_URL:
        raise ValueError("LINKEDIN_PROFILE_URL secret is not set.")

    username = extract_username_from_url(LINKEDIN_PROFILE_URL)
    if not username:
        raise ValueError(f"Cannot extract username from: {LINKEDIN_PROFILE_URL}")

    print("🚀 Starting LinkedIn scraper (Robust Navigation + Interceptor) …")
    print(f"   Target Username: {username}")

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

        # Attach network response listener to capture API JSON payloads
        tracker = NetworkPostTracker(username)
        page.on("response", tracker.handle_response)

        posts = []
        try:
            # Step 1: Login check & session warmup
            verify_and_warmup_session(page)

            # Step 2: Robust Navigation to Profile & Activity tab
            navigate_to_user_activity(page, username)

            # Step 3: Check captured posts from network response interceptor
            print(f"\n   Checking Network Interceptor results: {len(tracker.captured_posts)} posts captured")
            if tracker.captured_posts:
                posts = tracker.captured_posts[:6]

            # Step 4: DOM extraction fallback if needed
            if not posts:
                print("   Running DOM extraction fallback ...")
                posts = extract_posts_from_dom(page, username)

            # Step 5: Check and auto-refresh cookie secret
            fresh_cookie = extract_fresh_li_at(context)
            if fresh_cookie and fresh_cookie != LI_AT_COOKIE:
                print("   🔄 Cookie refreshed by LinkedIn — saving to GitHub secret ...")
                refresh_github_secret("LINKEDIN_LI_AT", fresh_cookie)
            elif fresh_cookie:
                print("   ✅ Session cookie verified")

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
        print("⚠️ 0 posts scraped — check debug screenshots in workflow artifacts.")


if __name__ == "__main__":
    scrape()
