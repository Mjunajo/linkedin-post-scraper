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
# HTML CODE-BLOCK PARSER
# ═══════════════════════════════════════════════════════════════

def parse_code_blocks(html_content: str) -> tuple[list, list]:
    """
    Returns (request_blocks, response_blocks) from LinkedIn's embedded JSON code tags.
    request_blocks: list of {"request": "/voyager/api/...", "queryId": "..."}
    response_blocks: list of parsed JSON dicts
    """
    raw_blocks = re.findall(r'<code[^>]*>(.*?)</code>', html_content, re.DOTALL)
    requests_found = []
    responses_found = []

    for raw in raw_blocks:
        cleaned = raw.strip()
        if cleaned.startswith("<!--") and cleaned.endswith("-->"):
            cleaned = cleaned[4:-3].strip()
        cleaned = html.unescape(cleaned)
        if not (cleaned.startswith("{") or cleaned.startswith("[")):
            continue
        try:
            data = json.loads(cleaned)
        except Exception:
            continue

        if isinstance(data, dict) and "request" in data and "method" in data:
            req_url = data.get("request", "")
            query_id = ""
            m = re.search(r'queryId=([A-Za-z0-9_.]+)', req_url)
            if m:
                query_id = m.group(1)
            requests_found.append({
                "url":      req_url,
                "queryId":  query_id,
                "headers":  data.get("headers", {}),
            })
        else:
            responses_found.append(data)

    return requests_found, responses_found


def extract_profile_urn(response_blocks: list, username: str) -> str:
    """
    Search response blocks for David's fsd_profile URN.
    It appears in the identityDashProfilesByMemberIdentity block.
    """
    for block in response_blocks:
        block_str = json.dumps(block)
        # Find all fsd_profile URNs in this block
        urns = re.findall(r'urn:li:fsd_profile:[A-Za-z0-9_-]+', block_str)
        # Also find member URNs
        member_urns = re.findall(r'urn:li:member:\d+', block_str)

        if urns and ("identityDashProfiles" in block_str or username in block_str):
            urn = urns[0]
            print(f"   Extracted profile URN: {urn}")
            return urn
        if member_urns and username in block_str:
            urn = member_urns[0]
            print(f"   Extracted member URN: {urn}")
            return urn
    return ""


def extract_profile_graphql_url(request_blocks: list, username: str) -> str:
    """
    Find the full GraphQL URL used to load David's profile.
    We'll re-call this to confirm the GraphQL works, then adapt for activity.
    """
    for req in request_blocks:
        url = req["url"]
        if (f"vanityName:{username}" in url or f"memberIdentity:{username}" in url) \
                and "graphql" in url:
            print(f"   Found profile GraphQL request: {url[:100]}")
            return url
    return ""


# ═══════════════════════════════════════════════════════════════
# JS BUNDLE QUERYID DISCOVERY
# ═══════════════════════════════════════════════════════════════

# Known query name patterns for activity/posts (sorted by priority)
ACTIVITY_QUERY_NAMES = [
    "voyagerIdentityDashProfileUpdates",
    "voyagerIdentityDashProfileUpdatesByMemberIdentity",
    "voyagerIdentityDashMemberProfileUpdates",
    "voyagerFeedDashProfileUpdates",
]

def find_activity_query_id_in_js(session: requests.Session, html_content: str) -> str:
    """
    Download LinkedIn JS bundles and search for the activity queryId hash.
    This is how services like Proxycurl/RapidAPI discover the current queryId.
    """
    # Extract script URLs with robust regex matching
    raw_matches = re.findall(r'src=["\']([^"\']+\.js[^"\']*)["\']', html_content, re.IGNORECASE)
    script_urls = []
    for u in raw_matches:
        if "licdn" in u or "/sc/" in u or "voyager" in u or "static" in u:
            full_u = u if u.startswith("http") else f"https://www.linkedin.com{u}" if u.startswith("/") else f"https://static.licdn.com/{u}"
            if full_u not in script_urls:
                script_urls.append(full_u)

    print(f"   Found {len(script_urls)} JS bundles to scan for queryId")

    all_found_qids = set()
    for url in script_urls[:25]:
        try:
            r = session.get(url, timeout=30)
            if r.status_code != 200:
                continue
            js = r.text
            # Look for any voyager queryId in JS
            qids = re.findall(r'["\']?(voyager[A-Za-z0-9]+\.[a-f0-9]{16,40})["\']?', js)
            for q in qids:
                all_found_qids.add(q)
                if any(name in q for name in ACTIVITY_QUERY_NAMES):
                    print(f"   ✅ Found target activity queryId in JS: {q}")
                    return q
        except Exception as e:
            print(f"   JS scan error for {url[-50:]}: {e}")

    if all_found_qids:
        print(f"   Found {len(all_found_qids)} generic queryIds in JS: {list(all_found_qids)[:5]}")

    print("   ⚠️ Could not find activity queryId in JS bundles")
    return ""



# ═══════════════════════════════════════════════════════════════
# VOYAGER API RESPONSE PARSER
# ═══════════════════════════════════════════════════════════════

SKIP_PREFIXES = (
    "urn:", "com.linkedin", "http", "AQ", "AAZ", "ajax:",
    "Web Developer", "Experience", "Education", "Contact info",
)


def _extract_text(item: dict) -> str:
    # commentary.text.text  (most common post format)
    commentary = item.get("commentary")
    if isinstance(commentary, dict):
        t = commentary.get("text")
        if isinstance(t, dict):
            v = t.get("text")
            if isinstance(v, str):
                return v.strip()
        elif isinstance(t, str):
            return t.strip()
    # description.text
    desc = item.get("description")
    if isinstance(desc, dict):
        t = desc.get("text")
        if isinstance(t, str):
            return t.strip()
    elif isinstance(desc, str):
        return desc.strip()
    # direct text field
    text_val = item.get("text")
    if isinstance(text_val, dict):
        t = text_val.get("text")
        if isinstance(t, str):
            return t.strip()
    elif isinstance(text_val, str):
        return text_val.strip()
    return ""


def _extract_date(item: dict) -> str:
    for k in ("publishedAt", "createdAt", "postedAt", "created"):
        v = item.get(k)
        if isinstance(v, (int, float)) and v > 1_000_000_000:
            return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(v / 1000))
        elif isinstance(v, str) and len(v) > 5:
            return v
    return ""


def _extract_image(item: dict) -> str:
    content = item.get("content")
    if isinstance(content, dict):
        url = content.get("url") or content.get("rootUrl")
        if url:
            return str(url)
    return ""


def _extract_post_url(item: dict, username: str) -> str:
    urn = item.get("entityUrn") or item.get("urn") or ""
    for marker in ("activity:", "share:", "ugcPost:"):
        if marker in urn:
            act_id = urn.split(":")[-1]
            return f"https://www.linkedin.com/feed/update/urn:li:activity:{act_id}/"
    return f"https://www.linkedin.com/in/{username}/recent-activity/all/"


def parse_api_response(data: dict, username: str) -> list[dict]:
    posts = []
    seen  = set()

    items = []
    if isinstance(data, dict):
        # Top-level included (Voyager REST)
        items.extend(data.get("included", []) or [])
        top = data.get("data") or {}
        if isinstance(top, dict):
            # data.elements (collection)
            items.extend(top.get("elements", []) or [])
            # data.data (GraphQL double-wrapper)
            inner = top.get("data")
            if isinstance(inner, dict):
                items.append(inner)
                for v in inner.values():
                    if isinstance(v, dict):
                        items.extend(v.get("elements", []) or [])
                    elif isinstance(v, list):
                        items.extend(v)
    elif isinstance(data, list):
        items.extend(data)

    for item in items:
        if not isinstance(item, dict):
            continue

        text = _extract_text(item)
        # Try nested dicts if not found at top level
        if not text:
            for k in ("value", "actor", "content", "updateMetadata"):
                nested = item.get(k)
                if isinstance(nested, dict):
                    text = _extract_text(nested)
                    if text:
                        break

        if not text or len(text) < 20 or text in seen:
            continue
        if any(text.startswith(p) for p in SKIP_PREFIXES):
            continue

        seen.add(text)
        posts.append({
            "text":      text,
            "date":      _extract_date(item),
            "image_url": _extract_image(item),
            "post_url":  _extract_post_url(item, username),
        })
        if len(posts) >= 6:
            break

    return posts


# ═══════════════════════════════════════════════════════════════
# SESSION & CSRF
# ═══════════════════════════════════════════════════════════════

def build_session() -> requests.Session:
    s = requests.Session()
    s.cookies.set("li_at", LI_AT_COOKIE, domain=".linkedin.com")
    return s


def get_csrf_and_html(session: requests.Session, username: str) -> tuple[str, str]:
    """
    Load the activity page. Returns (csrf_token, html_content).
    One HTTP request that gives us:
    - JSESSIONID cookie (= CSRF token)
    - All 19 code blocks with embedded API data
    - Script URLs for queryId discovery
    """
    url = f"https://www.linkedin.com/in/{username}/recent-activity/all/"
    print(f"   Loading activity page: {url}")
    try:
        resp = session.get(
            url,
            headers={**BROWSER_HEADERS, "Accept": "text/html,application/xhtml+xml,*/*;q=0.8"},
            timeout=30,
        )
        print(f"   Status: {resp.status_code} | Size: {len(resp.text):,} bytes")

        # Auto-refresh cookie if updated
        for c in session.cookies:
            if c.name == "li_at" and c.value and c.value != LI_AT_COOKIE:
                refresh_github_secret("LINKEDIN_LI_AT", c.value)
                break

        csrf = ""
        for c in session.cookies:
            if c.name == "JSESSIONID":
                csrf = c.value.strip('"')
                break

        return csrf, resp.text if resp.status_code == 200 else ""
    except Exception as e:
        print(f"   ⚠️ Page load error: {e}")
        return "", ""


def api_headers(csrf: str, referer: str) -> dict:
    return {
        **BROWSER_HEADERS,
        "Accept":                    "application/vnd.linkedin.normalized+json+2.1",
        "csrf-token":                csrf,
        "x-restli-protocol-version": "2.0.0",
        "x-li-lang":                 "en_US",
        "x-li-track": json.dumps({
            "clientVersion":  "1.13.13993",
            "mpVersion":      "1.13.13993",
            "osName":         "web",
            "timezoneOffset": -5,
            "timezone":       "America/New_York",
            "deviceFormFactor": "DESKTOP",
            "mpName":         "voyager-web",
            "displayDensity": 1,
            "displayWidth":   1366,
            "displayHeight":  768,
        }),
        "Sec-Fetch-Dest": "empty",
        "Sec-Fetch-Mode": "cors",
        "Sec-Fetch-Site": "same-origin",
        "Referer":  referer,
        "Origin":   "https://www.linkedin.com",
    }


# ═══════════════════════════════════════════════════════════════
# GRAPHQL API CALLS
# ═══════════════════════════════════════════════════════════════

def call_graphql(session: requests.Session, label: str, query_id: str,
                 variables: str, csrf: str, username: str) -> list[dict]:
    url = "https://www.linkedin.com/voyager/api/graphql"
    params = {
        "includeWebMetadata": "true",
        "variables":          variables,
        "queryId":            query_id,
    }
    hdrs = {**api_headers(csrf, f"https://www.linkedin.com/in/{username}/recent-activity/all/"),
            "Accept": "application/json"}
    try:
        r = session.get(url, headers=hdrs, params=params, timeout=30)
        print(f"   [{label}] {r.status_code}  queryId={query_id[:50]}")
        if r.status_code != 200:
            print(f"           Response: {r.text[:300]}")
            return []
        with open(f"graphql_{label}.json", "w") as f:
            f.write(r.text[:80_000])
        return parse_api_response(r.json(), username)
    except Exception as e:
        print(f"   [{label}] Error: {e}")
        return []


# ═══════════════════════════════════════════════════════════════
# MAIN SCRAPE
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
    print(f"   Target: {username}")

    session = build_session()

    # ── Step 1: Load activity page (1 request) ───────────────────────────────
    csrf, html_content = get_csrf_and_html(session, username)

    if not html_content:
        print("❌ Failed to load activity page.")
    else:
        print(f"   ✅ CSRF: {csrf[:20]}…" if csrf else "   ⚠️ No CSRF token")

    # ── Step 2: Parse embedded code blocks ───────────────────────────────────
    request_blocks, response_blocks = parse_code_blocks(html_content) if html_content else ([], [])
    print(f"   Parsed {len(request_blocks)} request blocks, {len(response_blocks)} response blocks")

    print("\n   --- Embedded Request Blocks ---")
    for req in request_blocks:
        print(f"   req: {req.get('url', '')[:110]}")

    # Extract David's profile URN from embedded response data
    profile_urn = extract_profile_urn(response_blocks, username)

    # Extract profile GraphQL URL used by the page
    profile_gql_url = extract_profile_graphql_url(request_blocks, username)

    # Try parsing posts directly from response blocks embedded in HTML
    posts = []
    for resp_block in response_blocks:
        found_p = parse_api_response(resp_block, username)
        if found_p:
            print(f"   ✅ Extracted {len(found_p)} posts directly from embedded HTML code block!")
            posts.extend(found_p)
            if len(posts) >= 6:
                break

    # ── Step 3: Discover activity queryId from JS bundles ────────────────────
    activity_query_id = ""
    if not posts and html_content:
        activity_query_id = find_activity_query_id_in_js(session, html_content)

    # ── Step 4: Call GraphQL with discovered queryId ──────────────────────────
    if not posts and activity_query_id and csrf:
        # Try with vanity name
        variables_vanity = f"(vanityName:{username},count:6,start:0)"
        posts = call_graphql(session, "activity_vanity", activity_query_id,
                             variables_vanity, csrf, username)

        if not posts:
            # Try with profile URN if we have it
            if profile_urn:
                variables_urn = f"(memberIdentity:{profile_urn},count:6,start:0)"
                posts = call_graphql(session, "activity_urn", activity_query_id,
                                     variables_urn, csrf, username)

        if not posts:
            variables_member = f"(memberIdentity:{username},count:6,start:0)"
            posts = call_graphql(session, "activity_member", activity_query_id,
                                 variables_member, csrf, username)

    # ── Step 5: Fallback — try the profile queryId with activity variables ────
    if not posts and profile_gql_url:
        profile_query_id = ""
        m = re.search(r'queryId=([A-Za-z0-9_.]+)', profile_gql_url)
        if m:
            profile_query_id = m.group(1)

        if profile_query_id and csrf:
            variables = f"(memberIdentity:{username},count:6,start:0)"
            posts = call_graphql(session, "profile_qid_activity", profile_query_id,
                                 variables, csrf, username)


    # ── Final output ──────────────────────────────────────────────────────────
    if not posts:
        print("\n⚠️  0 posts scraped. Check graphql_*.json artifacts for API response details.")

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
