import json
import time
import random
import os

from playwright.sync_api import sync_playwright, TimeoutError as PWTimeout
from playwright_stealth import stealth_sync

# ─── CREDENTIALS ───────────────────────────────────────────────────────────────
LINKEDIN_EMAIL       = os.environ.get("LINKEDIN_EMAIL", "")
LINKEDIN_PASSWORD    = os.environ.get("LINKEDIN_PASSWORD", "")
LINKEDIN_PROFILE_URL = os.environ.get("LINKEDIN_PROFILE_URL", "")
# ──────────────────────────────────────────────────────────────────────────────

# Increase all default timeouts to 60 seconds
DEFAULT_TIMEOUT = 60_000   # ms


def human_delay(min_s: float = 2.0, max_s: float = 5.0) -> None:
    time.sleep(random.uniform(min_s, max_s))


def scroll_slowly(page, steps: int = 5) -> None:
    for _ in range(steps):
        page.evaluate("window.scrollBy(0, window.innerHeight * 0.75)")
        human_delay(1.5, 3.0)


def save_debug_screenshot(page, name: str = "debug") -> None:
    """Save a screenshot so you can see what the browser is looking at."""
    try:
        path = f"{name}.png"
        page.screenshot(path=path, full_page=False)
        print(f"   📸 Screenshot saved: {path}")
    except Exception as e:
        print(f"   ⚠️  Could not save screenshot: {e}")


def dismiss_cookie_banner(page) -> None:
    """Dismiss any cookie consent overlay LinkedIn may show."""
    cookie_selectors = [
        "button[action-type='ACCEPT']",
        "button.artdeco-global-alert__action",
        "[data-tracking-control-name='cookie-consent-accept']",
        "button:has-text('Accept')",
        "button:has-text('Allow')",
        "button:has-text('Agree')",
    ]
    for sel in cookie_selectors:
        try:
            btn = page.locator(sel).first
            if btn.is_visible(timeout=3_000):
                btn.click()
                human_delay(1, 2)
                print("   ✅ Dismissed cookie/consent banner")
                return
        except Exception:
            continue


def do_login(page) -> None:
    """Navigate to LinkedIn login and fill credentials."""

    # ── 1. Go to login page ────────────────────────────────────────────────────
    print("   Opening LinkedIn login page …")
    page.goto("https://www.linkedin.com/login", wait_until="domcontentloaded", timeout=DEFAULT_TIMEOUT)

    # Give JS time to settle
    human_delay(3, 5)

    # Dismiss any cookie banner that blocks the form
    dismiss_cookie_banner(page)
    human_delay(1, 2)

    # ── 2. Wait for the email field with multiple fallback selectors ───────────
    print("   Waiting for login form …")
    username_selectors = [
        "#username",
        "input[name='session_key']",
        "input[autocomplete='username']",
        "input[type='email']",
    ]

    username_field = None
    for sel in username_selectors:
        try:
            page.wait_for_selector(sel, state="visible", timeout=15_000)
            username_field = sel
            print(f"   Found email field via: {sel}")
            break
        except PWTimeout:
            print(f"   Selector not found: {sel} — trying next …")
            continue

    if not username_field:
        save_debug_screenshot(page, "login_page_not_found")
        raise RuntimeError(
            "❌ Could not find the LinkedIn login form. "
            "See login_page_not_found.png for what the browser saw. "
            "LinkedIn may be showing a CAPTCHA or different page layout."
        )

    # ── 3. Fill credentials ────────────────────────────────────────────────────
    print("   Entering email …")
    page.click(username_field)
    human_delay(0.5, 1.0)
    page.fill(username_field, LINKEDIN_EMAIL)
    human_delay(0.8, 1.5)

    password_selectors = [
        "#password",
        "input[name='session_password']",
        "input[type='password']",
    ]

    password_field = None
    for sel in password_selectors:
        try:
            page.wait_for_selector(sel, state="visible", timeout=8_000)
            password_field = sel
            break
        except PWTimeout:
            continue

    if not password_field:
        save_debug_screenshot(page, "password_field_not_found")
        raise RuntimeError("❌ Could not find the LinkedIn password field.")

    print("   Entering password …")
    page.click(password_field)
    human_delay(0.5, 1.0)
    page.fill(password_field, LINKEDIN_PASSWORD)
    human_delay(1.0, 2.0)

    # ── 4. Submit ──────────────────────────────────────────────────────────────
    print("   Clicking Sign In …")
    submit_selectors = [
        "button[type='submit']",
        "button[data-litms-control-urn='login-submit']",
        ".login__form_action_container button",
    ]
    for sel in submit_selectors:
        try:
            btn = page.locator(sel).first
            if btn.is_visible(timeout=5_000):
                btn.click()
                break
        except Exception:
            continue

    # Wait for navigation after login
    page.wait_for_load_state("domcontentloaded", timeout=DEFAULT_TIMEOUT)
    human_delay(4, 7)

    # ── 5. Check for security checkpoint ──────────────────────────────────────
    current_url = page.url
    print(f"   Post-login URL: {current_url}")

    if "checkpoint" in current_url or "challenge" in current_url:
        save_debug_screenshot(page, "security_checkpoint")
        raise RuntimeError(
            "🚫 LinkedIn is asking for a security verification (CAPTCHA / phone/email check). "
            "Please log into this LinkedIn account manually in a real browser, "
            "complete the verification, then re-run the GitHub Action."
        )

    if "feed" in current_url or "mynetwork" in current_url or "linkedin.com/in/" in current_url:
        print("   ✅ Logged in successfully!")
    else:
        save_debug_screenshot(page, "unexpected_post_login_page")
        print(f"   ⚠️  Unexpected URL after login: {current_url} — continuing anyway …")


def extract_posts(page) -> list[dict]:
    """Extract up to 6 posts from the current page."""
    posts = []

    # Try multiple container selectors LinkedIn has used over time
    container_selectors = [
        ".feed-shared-update-v2",
        ".occludable-update",
        "[data-urn*='activity']",
        ".profile-creator-shared-feed-update__container",
    ]

    post_elements = []
    for sel in container_selectors:
        try:
            page.wait_for_selector(sel, timeout=15_000)
            post_elements = page.query_selector_all(sel)
            if post_elements:
                print(f"   Found {len(post_elements)} elements via: {sel}")
                break
        except PWTimeout:
            print(f"   Selector not found: {sel}")
            continue

    if not post_elements:
        save_debug_screenshot(page, "no_posts_found")
        print("   ⚠️  Could not find any post elements — see no_posts_found.png")
        return posts

    for el in post_elements[:12]:  # scan up to 12 to find 6 real ones
        try:
            # Skip ads / sponsored
            sponsored = el.query_selector(".feed-shared-actor__sub-description")
            if sponsored and "promoted" in (sponsored.inner_text() or "").lower():
                continue

            # Post text — try multiple selectors
            text_el = (
                el.query_selector(".feed-shared-update-v2__description")
                or el.query_selector(".feed-shared-text")
                or el.query_selector(".break-words")
                or el.query_selector("[data-test-id='main-feed-activity-card__commentary']")
            )

            raw_text = text_el.inner_text().strip() if text_el else ""
            if not raw_text:
                continue

            # Date
            time_el  = el.query_selector("time")

            # Image
            image_el = (
                el.query_selector(".feed-shared-image__image")
                or el.query_selector(".update-components-image__image")
                or el.query_selector("img.ivm-view-attr__img--centered")
            )

            # Post link
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
            continue

    return posts


def scrape() -> None:
    if not LINKEDIN_EMAIL or not LINKEDIN_PASSWORD or not LINKEDIN_PROFILE_URL:
        raise ValueError(
            "Missing env vars. Set LINKEDIN_EMAIL, LINKEDIN_PASSWORD, LINKEDIN_PROFILE_URL."
        )

    print("🚀 Starting LinkedIn scraper …")

    with sync_playwright() as p:
        browser = p.chromium.launch(
            headless=True,
            args=[
                "--no-sandbox",
                "--disable-setuid-sandbox",
                "--disable-blink-features=AutomationControlled",
                "--disable-infobars",
                "--disable-extensions",
                "--window-size=1366,768",
                "--start-maximized",
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
            accept_downloads=False,
        )

        # Set a longer default timeout on the context level
        context.set_default_timeout(DEFAULT_TIMEOUT)

        page = context.new_page()

        # Apply stealth patches
        stealth_sync(page)

        try:
            # ── LOGIN ──────────────────────────────────────────────────────────
            do_login(page)

            # ── NAVIGATE TO CLIENT PROFILE ─────────────────────────────────────
            print(f"   Navigating to: {LINKEDIN_PROFILE_URL}")
            page.goto(LINKEDIN_PROFILE_URL, wait_until="domcontentloaded", timeout=DEFAULT_TIMEOUT)
            human_delay(4, 7)

            save_debug_screenshot(page, "profile_page_loaded")

            # ── SCROLL TO LOAD POSTS ───────────────────────────────────────────
            print("   Scrolling to load posts …")
            scroll_slowly(page, steps=5)

            # ── EXTRACT ────────────────────────────────────────────────────────
            print("   Extracting posts …")
            posts = extract_posts(page)

        except Exception as e:
            save_debug_screenshot(page, "error_state")
            browser.close()
            raise e

        browser.close()

    # ── WRITE OUTPUT ───────────────────────────────────────────────────────────
    output = {
        "updated_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "post_count": len(posts),
        "posts":      posts,
    }

    with open("posts.json", "w", encoding="utf-8") as f:
        json.dump(output, f, ensure_ascii=False, indent=2)

    print(f"✅ Done! Scraped {len(posts)} posts → posts.json")

    if len(posts) == 0:
        print("⚠️  WARNING: 0 posts scraped. Check the debug screenshots uploaded as artifacts.")


if __name__ == "__main__":
    scrape()
