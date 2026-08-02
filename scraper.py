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

DEFAULT_TIMEOUT = 60_000  # ms


def human_delay(min_s: float = 2.0, max_s: float = 5.0) -> None:
    time.sleep(random.uniform(min_s, max_s))


def scroll_slowly(page, steps: int = 5) -> None:
    for _ in range(steps):
        page.evaluate("window.scrollBy(0, window.innerHeight * 0.75)")
        human_delay(1.5, 3.0)


def save_debug_screenshot(page, name: str = "debug") -> None:
    try:
        page.screenshot(path=f"{name}.png", full_page=True)
        print(f"   📸 Screenshot: {name}.png")
    except Exception as e:
        print(f"   ⚠️  Screenshot failed: {e}")


def print_page_info(page, label: str = "") -> None:
    try:
        print(f"   [{label}] URL:   {page.url}")
        print(f"   [{label}] Title: {page.title()}")
    except Exception:
        pass


def fill_login_form(page) -> None:
    """
    Fill the LinkedIn login form.
    Uses label/placeholder selectors that match LinkedIn's current (2024-2025) login page design
    which shows 'Email or phone' and 'Password' labels — the old id='username' is gone.
    """

    # ── Email field ────────────────────────────────────────────────────────────
    # Try in order: get_by_label (most reliable), get_by_placeholder, CSS fallbacks
    email_locator = None

    # Strategy 1: by visible label text (matches the 'Email or phone' label)
    try:
        loc = page.get_by_label("Email or phone")
        loc.wait_for(state="visible", timeout=15_000)
        email_locator = loc
        print("   ✅ Email field found via label 'Email or phone'")
    except Exception:
        pass

    # Strategy 2: by placeholder
    if not email_locator:
        try:
            loc = page.get_by_placeholder("Email or phone")
            loc.wait_for(state="visible", timeout=8_000)
            email_locator = loc
            print("   ✅ Email field found via placeholder")
        except Exception:
            pass

    # Strategy 3: CSS selectors (old + new)
    if not email_locator:
        css_selectors = [
            "#username",
            "input[name='session_key']",
            "input[autocomplete='username']",
            "input[type='email']",
            "input[type='text']",
            "form input:not([type='password']):not([type='hidden'])",
        ]
        for sel in css_selectors:
            try:
                page.wait_for_selector(sel, state="visible", timeout=6_000)
                email_locator = page.locator(sel).first
                print(f"   ✅ Email field found via CSS: {sel}")
                break
            except PWTimeout:
                print(f"   — CSS selector missed: {sel}")
                continue

    if not email_locator:
        save_debug_screenshot(page, "email_field_not_found")
        raise RuntimeError(
            "❌ Could not find the email/phone input field.\n"
            "See email_field_not_found.png for what the browser showed."
        )

    print("   Typing email …")
    email_locator.click()
    human_delay(0.5, 1.0)
    email_locator.type(LINKEDIN_EMAIL, delay=random.randint(60, 130))
    human_delay(1.0, 2.0)

    # ── Password field ─────────────────────────────────────────────────────────
    password_locator = None

    try:
        loc = page.get_by_label("Password")
        loc.wait_for(state="visible", timeout=8_000)
        password_locator = loc
        print("   ✅ Password field found via label 'Password'")
    except Exception:
        pass

    if not password_locator:
        pw_selectors = [
            "input[type='password']",
            "#password",
            "input[name='session_password']",
            "input[autocomplete='current-password']",
        ]
        for sel in pw_selectors:
            try:
                page.wait_for_selector(sel, state="visible", timeout=6_000)
                password_locator = page.locator(sel).first
                print(f"   ✅ Password field found via CSS: {sel}")
                break
            except PWTimeout:
                continue

    if not password_locator:
        save_debug_screenshot(page, "password_field_not_found")
        raise RuntimeError("❌ Could not find the password input field.")

    print("   Typing password …")
    password_locator.click()
    human_delay(0.5, 1.0)
    password_locator.type(LINKEDIN_PASSWORD, delay=random.randint(60, 130))
    human_delay(1.0, 2.0)

    # ── Submit button ──────────────────────────────────────────────────────────
    print("   Clicking Sign in …")
    submitted = False

    # Try 'Sign in' button by text first (most reliable on new layout)
    try:
        btn = page.get_by_role("button", name="Sign in")
        if btn.is_visible(timeout=5_000):
            btn.click()
            submitted = True
            print("   ✅ Clicked 'Sign in' button via role+name")
    except Exception:
        pass

    if not submitted:
        submit_selectors = [
            "button[type='submit']",
            "button[data-litms-control-urn='login-submit']",
            ".login__form_action_container button",
            "button:has-text('Sign in')",
        ]
        for sel in submit_selectors:
            try:
                btn = page.locator(sel).first
                if btn.is_visible(timeout=4_000):
                    btn.click()
                    submitted = True
                    break
            except Exception:
                continue

    if not submitted:
        print("   ⚠️  No submit button found — pressing Enter as fallback")
        page.keyboard.press("Enter")


def do_login(page) -> None:
    """Navigate to LinkedIn login page and authenticate."""

    print("   Opening LinkedIn login page …")
    page.goto(
        "https://www.linkedin.com/login",
        wait_until="load",
        timeout=DEFAULT_TIMEOUT,
    )
    human_delay(4, 6)
    print_page_info(page, "login-page")
    save_debug_screenshot(page, "01_login_page_loaded")

    # If already redirected to feed, we're already logged in
    if "feed" in page.url or "mynetwork" in page.url:
        print("   ✅ Already logged in!")
        return

    # Fill credentials
    fill_login_form(page)

    # Wait for post-login navigation
    print("   Waiting for post-login redirect …")
    page.wait_for_load_state("domcontentloaded", timeout=DEFAULT_TIMEOUT)
    human_delay(5, 8)
    print_page_info(page, "post-login")
    save_debug_screenshot(page, "02_post_login")

    # Evaluate result
    url = page.url
    if "checkpoint" in url or "challenge" in url:
        raise RuntimeError(
            "🚫 LinkedIn security checkpoint (phone/email verification required).\n"
            "Log into this LinkedIn account manually in a real browser, "
            "complete the verification once, then re-run the GitHub Action."
        )

    if "login" in url and "feed" not in url:
        save_debug_screenshot(page, "02b_login_failed")
        raise RuntimeError(
            "❌ Still on login page after submitting. "
            "Check credentials in GitHub Secrets (LINKEDIN_EMAIL / LINKEDIN_PASSWORD)."
        )

    print("   ✅ Login successful!")


def extract_posts(page) -> list[dict]:
    posts = []

    post_selectors = [
        ".feed-shared-update-v2",
        ".occludable-update",
        "[data-urn*='activity']",
        ".profile-creator-shared-feed-update__container",
        ".ember-view.occludable-update",
    ]

    post_elements = []
    for sel in post_selectors:
        try:
            page.wait_for_selector(sel, timeout=15_000)
            post_elements = page.query_selector_all(sel)
            if post_elements:
                print(f"   Found {len(post_elements)} post elements via: {sel}")
                break
        except PWTimeout:
            print(f"   — Post selector not found: {sel}")
            continue

    if not post_elements:
        save_debug_screenshot(page, "04_no_posts_found")
        print("   ⚠️  No post elements found — see 04_no_posts_found.png")
        return posts

    for el in post_elements[:14]:
        try:
            # Skip sponsored posts
            sponsored = el.query_selector(".feed-shared-actor__sub-description")
            if sponsored and "promoted" in (sponsored.inner_text() or "").lower():
                continue

            text_el = (
                el.query_selector(".feed-shared-update-v2__description")
                or el.query_selector(".feed-shared-text")
                or el.query_selector(".break-words")
                or el.query_selector("[data-test-id='main-feed-activity-card__commentary']")
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
            continue

    return posts


def scrape() -> None:
    if not LINKEDIN_EMAIL or not LINKEDIN_PASSWORD or not LINKEDIN_PROFILE_URL:
        raise ValueError(
            "Missing env vars. Set LINKEDIN_EMAIL, LINKEDIN_PASSWORD, "
            "LINKEDIN_PROFILE_URL in GitHub Secrets."
        )

    print("🚀 Starting LinkedIn scraper …")
    print(f"   Target profile: {LINKEDIN_PROFILE_URL}")

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

        page = context.new_page()
        stealth_sync(page)

        try:
            # ── STEP 1: Login ──────────────────────────────────────────────────
            do_login(page)

            # ── STEP 2: Navigate to client profile ────────────────────────────
            print(f"\n   Navigating to: {LINKEDIN_PROFILE_URL}")
            page.goto(LINKEDIN_PROFILE_URL, wait_until="domcontentloaded", timeout=DEFAULT_TIMEOUT)
            human_delay(4, 7)
            print_page_info(page, "profile")
            save_debug_screenshot(page, "03_profile_page")

            # ── STEP 3: Scroll to load posts ───────────────────────────────────
            print("   Scrolling to load posts …")
            scroll_slowly(page, steps=5)
            save_debug_screenshot(page, "03b_after_scroll")

            # ── STEP 4: Extract posts ──────────────────────────────────────────
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

    print(f"\n✅ Done! Scraped {len(posts)} posts → posts.json")
    if len(posts) == 0:
        print("⚠️  0 posts scraped — check screenshots in debug-screenshots artifact.")


if __name__ == "__main__":
    scrape()
