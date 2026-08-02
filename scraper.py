import json
import time
import random
import os

from playwright.sync_api import sync_playwright
from playwright_stealth import stealth_sync

# ─── CREDENTIALS (injected from GitHub Secrets or .env) ───────────────────────
LINKEDIN_EMAIL    = os.environ.get("LINKEDIN_EMAIL", "")
LINKEDIN_PASSWORD = os.environ.get("LINKEDIN_PASSWORD", "")
# Client's LinkedIn "recent activity / shares" page URL
# Format: https://www.linkedin.com/in/CLIENT-USERNAME/recent-activity/all/
LINKEDIN_PROFILE_URL = os.environ.get("LINKEDIN_PROFILE_URL", "")
# ──────────────────────────────────────────────────────────────────────────────


def human_delay(min_s: float = 2.0, max_s: float = 5.0) -> None:
    """Sleep for a random duration to mimic human behaviour."""
    time.sleep(random.uniform(min_s, max_s))


def scroll_slowly(page, steps: int = 4) -> None:
    """Gradually scroll down the page like a human reading content."""
    for _ in range(steps):
        page.evaluate("window.scrollBy(0, window.innerHeight * 0.7)")
        human_delay(1.5, 3.0)


def extract_posts(page) -> list[dict]:
    """Pull post data from the rendered page."""
    posts = []

    # Wait for at least one post card to appear
    try:
        page.wait_for_selector(
            ".feed-shared-update-v2, .occludable-update",
            timeout=15_000,
        )
    except Exception:
        print("⚠️  Could not find post elements — page structure may have changed.")
        return posts

    post_elements = page.query_selector_all(
        ".feed-shared-update-v2, .occludable-update"
    )

    print(f"   Found {len(post_elements)} post elements on page")

    for el in post_elements[:10]:  # scan up to 10 to get 6 real posts
        try:
            # Skip sponsored / ads
            sponsored = el.query_selector(".feed-shared-actor__sub-description")
            if sponsored and "promoted" in (sponsored.inner_text() or "").lower():
                continue

            # Post text
            text_el = (
                el.query_selector(".feed-shared-update-v2__description")
                or el.query_selector(".feed-shared-text")
                or el.query_selector("[data-test-id='main-feed-activity-card__commentary']")
            )

            # Timestamp / date
            time_el = el.query_selector("time")

            # Image (if any)
            image_el = (
                el.query_selector(".feed-shared-image__image")
                or el.query_selector(".update-components-image__image")
            )

            # Clickable link to the post
            link_el = el.query_selector("a.app-aware-link[href*='activity']")

            raw_text = text_el.inner_text().strip() if text_el else ""

            # Skip empty posts
            if not raw_text:
                continue

            post = {
                "text":      raw_text,
                "date":      time_el.get_attribute("datetime") if time_el else "",
                "image_url": image_el.get_attribute("src") if image_el else "",
                "post_url":  (link_el.get_attribute("href") if link_el else LINKEDIN_PROFILE_URL),
            }
            posts.append(post)

            if len(posts) == 6:
                break

        except Exception as e:
            print(f"   ⚠️  Skipping a post due to error: {e}")
            continue

    return posts


def scrape() -> None:
    if not LINKEDIN_EMAIL or not LINKEDIN_PASSWORD or not LINKEDIN_PROFILE_URL:
        raise ValueError(
            "Missing environment variables. "
            "Set LINKEDIN_EMAIL, LINKEDIN_PASSWORD, LINKEDIN_PROFILE_URL."
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
                "--window-size=1280,900",
            ],
        )

        context = browser.new_context(
            user_agent=(
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/125.0.0.0 Safari/537.36"
            ),
            viewport={"width": 1280, "height": 900},
            locale="en-US",
            timezone_id="America/New_York",
        )

        page = context.new_page()

        # Apply stealth patches to hide automation signals
        stealth_sync(page)

        # ── Step 1: Open LinkedIn login page ──────────────────────────────────
        print("   Opening LinkedIn login page …")
        page.goto("https://www.linkedin.com/login", wait_until="networkidle")
        human_delay(2, 4)

        # ── Step 2: Fill credentials ──────────────────────────────────────────
        print("   Entering credentials …")
        page.fill("#username", LINKEDIN_EMAIL)
        human_delay(0.8, 1.5)
        page.fill("#password", LINKEDIN_PASSWORD)
        human_delay(1.0, 2.0)

        # ── Step 3: Click Sign In ─────────────────────────────────────────────
        print("   Clicking Sign In …")
        page.click('[type="submit"]')
        page.wait_for_load_state("networkidle")
        human_delay(4, 7)

        # Detect security challenge / CAPTCHA
        if "checkpoint" in page.url or "challenge" in page.url:
            browser.close()
            raise RuntimeError(
                "🚫 LinkedIn is asking for a security challenge. "
                "Log into this LinkedIn account manually once to clear it, then retry."
            )

        print("   ✅ Logged in successfully")

        # ── Step 4: Navigate to client's activity page ────────────────────────
        print(f"   Navigating to: {LINKEDIN_PROFILE_URL}")
        page.goto(LINKEDIN_PROFILE_URL, wait_until="networkidle")
        human_delay(3, 6)

        # ── Step 5: Scroll to trigger lazy-loaded posts ───────────────────────
        print("   Scrolling to load posts …")
        scroll_slowly(page, steps=5)

        # ── Step 6: Extract posts ─────────────────────────────────────────────
        print("   Extracting posts …")
        posts = extract_posts(page)

        browser.close()

    # ── Step 7: Write output ──────────────────────────────────────────────────
    output = {
        "updated_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "post_count": len(posts),
        "posts": posts,
    }

    with open("posts.json", "w", encoding="utf-8") as f:
        json.dump(output, f, ensure_ascii=False, indent=2)

    print(f"✅ Done! Scraped {len(posts)} posts → posts.json")


if __name__ == "__main__":
    scrape()
