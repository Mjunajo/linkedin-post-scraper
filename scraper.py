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

# For auto-refresh: a GitHub Personal Access Token with repo scope
GH_PAT            = os.environ.get("GH_PAT", "")
GITHUB_REPOSITORY = os.environ.get("GITHUB_REPOSITORY", "")  # auto-set by GitHub Actions
# ──────────────────────────────────────────────────────────────────────────────

DEFAULT_TIMEOUT = 60_000  # ms


# ═══════════════════════════════════════════════════════════════
# HELPERS
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


# ═══════════════════════════════════════════════════════════════
# GITHUB SECRET AUTO-REFRESH
# ═══════════════════════════════════════════════════════════════

def _encrypt_secret_for_github(public_key_b64: str, secret_value: str) -> str:
    """
    Encrypt a secret value using the repo's public key.
    Required by GitHub API before updating a secret.
    """
    pk = nacl_public.PublicKey(public_key_b64.encode("utf-8"), encoding.Base64Encoder())
    sealed_box = nacl_public.SealedBox(pk)
    encrypted = sealed_box.encrypt(secret_value.encode("utf-8"))
    return base64.b64encode(encrypted).decode("utf-8")


def refresh_github_secret(secret_name: str, new_value: str) -> bool:
    """
    Update a GitHub Actions secret via the GitHub API.
    Requires GH_PAT (Personal Access Token) with repo scope.
    Returns True on success, False on failure.
    """
    if not GH_PAT or not GITHUB_REPOSITORY:
        print("   ⚠️  GH_PAT or GITHUB_REPOSITORY not set — skipping auto-refresh")
        return False

    headers = {
        "Authorization": f"Bearer {GH_PAT}",
        "Accept":        "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
    }
    base_url = f"https://api.github.com/repos/{GITHUB_REPOSITORY}/actions/secrets"

    # Step 1: Get repo public key (needed to encrypt the secret)
    key_resp = requests.get(f"{base_url}/public-key", headers=headers, timeout=10)
    if key_resp.status_code != 200:
        print(f"   ⚠️  Could not fetch GitHub public key: {key_resp.status_code}")
        return False

    key_data      = key_resp.json()
    encrypted_val = _encrypt_secret_for_github(key_data["key"], new_value)

    # Step 2: Push the updated secret
    put_resp = requests.put(
        f"{base_url}/{secret_name}",
        headers=headers,
        json={"encrypted_value": encrypted_val, "key_id": key_data["key_id"]},
        timeout=10,
    )

    if put_resp.status_code in (201, 204):
        print(f"   ✅ GitHub Secret '{secret_name}' updated automatically")
        return True
    else:
        print(f"   ⚠️  Secret update failed: {put_resp.status_code} — {put_resp.text}")
        return False


def extract_fresh_li_at(context) -> str:
    """
    After scraping, read the current li_at cookie value from the browser.
    LinkedIn often refreshes/extends the cookie on each use —
    we capture the latest value and save it back to GitHub Secrets.
    """
    cookies = context.cookies()
    for c in cookies:
        if c["name"] == "li_at" and "linkedin.com" in c.get("domain", ""):
            return c["value"]
    return ""


# ═══════════════════════════════════════════════════════════════
# SESSION
# ═══════════════════════════════════════════════════════════════

def inject_session_cookie(context) -> None:
    """Inject li_at directly — bypasses login form and CAPTCHA entirely."""
    context.add_cookies([
        {
            "name":     "li_at",
            "value":    LI_AT_COOKIE,
            "domain":   ".linkedin.com",
            "path":     "/",
            "httpOnly": True,
            "secure":   True,
            "sameSite": "None",
        },
        # JSESSIONID is also checked by LinkedIn on some requests
        # Leave blank — LinkedIn will issue one once the session is accepted
    ])
    print("   ✅ Session cookie injected (no login form, no CAPTCHA)")


def extract_username_from_url(url: str) -> str:
    """
    Extract the LinkedIn username/slug from any LinkedIn profile URL format.
    Handles:
      https://www.linkedin.com/in/username/
      https://www.linkedin.com/in/username/recent-activity/all/
      https://www.linkedin.com/in/username/recent-activity/shares/
    """
    match = re.search(r'linkedin\.com/in/([^/?#]+)', url)
    if match:
        return match.group(1).strip('/')
    return ""


def build_activity_url(username: str) -> str:
    """Build the canonical recent-activity URL for a given LinkedIn username."""
    return f"https://www.linkedin.com/in/{username}/recent-activity/all/"


def navigate_to_profile(page, username: str) -> None:
    """
    Navigate to the client's LinkedIn activity page in two steps:
    1. Go to the base profile page first (avoids redirect loops)
    2. Then go to the recent-activity page
    This prevents ERR_TOO_MANY_REDIRECTS caused by stale or malformed activity URLs.
    """
    base_url     = f"https://www.linkedin.com/in/{username}/"
    activity_url = build_activity_url(username)

    # Step 1: base profile
    print(f"   Step 1 — Loading base profile: {base_url}")
    try:
        page.goto(base_url, wait_until="domcontentloaded", timeout=DEFAULT_TIMEOUT)
    except Exception as e:
        print(f"   ⚠️  Base profile load warning (continuing): {e}")
    human_delay(3, 5)
    print_page_info(page, "base-profile")
    save_debug_screenshot(page, "02a_base_profile")

    # Step 2: activity page
    print(f"   Step 2 — Loading activity page: {activity_url}")
    try:
        page.goto(activity_url, wait_until="domcontentloaded", timeout=DEFAULT_TIMEOUT)
    except Exception as e:
        # Catch redirect errors — page may still have loaded partially
        print(f"   ⚠️  Activity page warning (will try to scrape anyway): {e}")
    human_delay(4, 6)
    print_page_info(page, "activity-page")
    save_debug_screenshot(page, "02b_activity_page")


def verify_session(page) -> None:
    """
    Verify the injected session is still valid.
    Uses the homepage (not /feed/) to avoid redirect loops when cookie is invalid.
    /feed/ causes ERR_TOO_MANY_REDIRECTS with a bad cookie because LinkedIn
    tries: feed → login → feed → login → … infinitely.
    The homepage always returns 200 and shows either the feed or the login page.
    """
    print("   Verifying session …")
    try:
        page.goto(
            "https://www.linkedin.com/",
            wait_until="domcontentloaded",
            timeout=DEFAULT_TIMEOUT,
        )
    except Exception as e:
        err = str(e)
        if "ERR_TOO_MANY_REDIRECTS" in err:
            raise RuntimeError(
                "❌ LinkedIn session cookie is INVALID or REVOKED.\n\n"
                "LinkedIn has invalidated this cookie (common after detecting automated access).\n"
                "You need a fresh cookie — it takes 2 minutes:\n"
                "  1. Open Chrome → go to linkedin.com → make sure you are logged in\n"
                "  2. Press F12 → Application tab → Cookies → linkedin.com\n"
                "  3. Find 'li_at' → copy its Value (long string starting with AQED...)\n"
                "  4. GitHub repo → Settings → Secrets → Actions\n"
                "  5. Update LINKEDIN_LI_AT with the new value\n"
                "  6. Re-run the GitHub Action\n"
            ) from None
        raise

    human_delay(3, 5)
    print_page_info(page, "session-check")
    save_debug_screenshot(page, "01_session_check")

    url = page.url
    title = page.title().lower()

    # Logged-in indicators
    if any(x in url for x in ("feed", "mynetwork", "/in/")) or "linkedin" in title:
        if "login" not in url and "signup" not in url and "authwall" not in url:
            print("   ✅ Session valid — logged in!")
            return

    # Not logged in
    if "login" in url or "authwall" in url or "signup" in url:
        raise RuntimeError(
            "❌ LinkedIn session cookie has EXPIRED.\n\n"
            "Get a fresh cookie (2 minutes):\n"
            "  1. Open Chrome → linkedin.com → log in with 'Keep me signed in' ✅\n"
            "  2. Press F12 → Application → Cookies → linkedin.com\n"
            "  3. Copy the Value of 'li_at'\n"
            "  4. Update LINKEDIN_LI_AT in GitHub Secrets\n"
            "  5. Re-run the workflow\n"
            "Note: With 'Keep me signed in', cookies last 12+ months."
        )

    # Unknown state — log it and continue
    print(f"   ⚠️  Unexpected URL after homepage load: {url} — continuing anyway")


# ═══════════════════════════════════════════════════════════════
# SCRAPING
# ═══════════════════════════════════════════════════════════════

def extract_posts(page) -> list[dict]:
    posts = []

    post_selectors = [
        ".feed-shared-update-v2",
        ".occludable-update",
        "[data-urn*='activity']",
        ".profile-creator-shared-feed-update__container",
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

    if not post_elements:
        save_debug_screenshot(page, "04_no_posts_found")
        print("   ⚠️  No post elements found — check 04_no_posts_found.png")
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
        raise ValueError("LINKEDIN_LI_AT secret is not set. See setup instructions.")
    if not LINKEDIN_PROFILE_URL:
        raise ValueError("LINKEDIN_PROFILE_URL secret is not set.")

    print("🚀 Starting LinkedIn scraper (cookie auth + auto-refresh) …")
    print(f"   Target: {LINKEDIN_PROFILE_URL}")

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

        # ── 1. Inject cookie (no login/CAPTCHA) ───────────────────────────────
        inject_session_cookie(context)

        page = context.new_page()
        stealth_sync(page)

        posts = []
        try:
            # ── 2. Verify session ─────────────────────────────────────────────
            verify_session(page)

            # ── 3. Navigate to client profile (two-step to avoid redirects) ───
            username = extract_username_from_url(LINKEDIN_PROFILE_URL)
            if not username:
                raise ValueError(
                    f"Could not extract LinkedIn username from LINKEDIN_PROFILE_URL.\n"
                    f"Make sure it looks like: https://www.linkedin.com/in/USERNAME/"
                )
            print(f"\n   Client LinkedIn username: {username}")
            navigate_to_profile(page, username)

            # ── 4. Scroll to trigger lazy-load ────────────────────────────────
            print("   Scrolling to load posts …")
            scroll_slowly(page, steps=5)
            save_debug_screenshot(page, "03_after_scroll")

            # ── 5. Extract posts ──────────────────────────────────────────────
            print("   Extracting posts …")
            posts = extract_posts(page)

            # ── 6. Auto-refresh the cookie in GitHub Secrets ──────────────────
            # After a successful scrape, LinkedIn may have issued a refreshed cookie.
            # We capture it and push it back to GitHub so the secret never expires.
            print("\n   Checking for refreshed session cookie …")
            fresh_li_at = extract_fresh_li_at(context)
            if fresh_li_at and fresh_li_at != LI_AT_COOKIE:
                print("   🔄 Cookie was refreshed by LinkedIn — saving new value …")
                refresh_github_secret("LINKEDIN_LI_AT", fresh_li_at)
            elif fresh_li_at == LI_AT_COOKIE:
                print("   ✅ Cookie unchanged — still fresh, no update needed")
            else:
                print("   ⚠️  Could not read fresh cookie from browser")

        except Exception as e:
            save_debug_screenshot(page, "error_state")
            browser.close()
            raise e

        browser.close()

    # ── 7. Write posts.json ────────────────────────────────────────────────────
    output = {
        "updated_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "post_count": len(posts),
        "posts":      posts,
    }
    with open("posts.json", "w", encoding="utf-8") as f:
        json.dump(output, f, ensure_ascii=False, indent=2)

    print(f"\n✅ Done! Scraped {len(posts)} posts → posts.json")
    if len(posts) == 0:
        print("⚠️  0 posts scraped — check debug screenshots in artifacts.")


if __name__ == "__main__":
    scrape()
