"""
LocalReach — FREE Google Maps Scraper (No API Key Required)
Uses Playwright to browse Google Maps like a human and extract
businesses without websites.

Setup:
  pip install playwright python-dotenv
  playwright install chromium

Usage:
  python scraper_free.py --query "restaurants in State College PA" --limit 60
  python scraper_free.py --query "auto repair State College PA" --limit 40 --headless
"""

import json
import csv
import time
import random
import argparse
import os
import re
from datetime import datetime

try:
    from playwright.sync_api import sync_playwright, TimeoutError as PlaywrightTimeout
except ImportError:
    print("Run: pip install playwright && playwright install chromium")
    exit(1)

CRM_JSON_PATH = "crm_leads.json"
CSV_OUTPUT    = "leads_export.csv"

# ── Helpers ───────────────────────────────────────────────────────────────────

def human_delay(lo=0.8, hi=2.2):
    """Random pause to avoid bot detection."""
    time.sleep(random.uniform(lo, hi))

def score_lead(has_website: bool, review_count: int, has_photos: bool) -> int:
    score = 30
    if not has_website:
        score += 35
    if review_count == 0:
        score += 20
    elif review_count < 10:
        score += 14
    elif review_count < 25:
        score += 8
    if not has_photos:
        score += 5
    if review_count > 100:
        score -= 20
    return min(100, max(0, score))

def detect_missing(has_website: bool, review_count: int, has_photos: bool) -> list:
    missing = []
    if not has_website:
        missing.append("website")
    if review_count < 15:
        missing.append("reviews")
    if not has_photos:
        missing.append("gmb")
    return missing

def parse_review_count(text: str) -> int:
    """Extract integer from strings like '(42)' or '4.2 (123 reviews)'"""
    if not text:
        return 0
    nums = re.findall(r'\d+', text.replace(",", ""))
    # We want the review count, not the rating
    for n in nums:
        val = int(n)
        if val > 5:   # ratings are ≤5, review counts are >5
            return val
    return 0

def fmt_phone(raw: str) -> str:
    digits = re.sub(r'\D', '', raw or "")
    if len(digits) == 10:
        return f"({digits[:3]}) {digits[3:6]}-{digits[6:]}"
    if len(digits) == 11 and digits[0] == "1":
        return f"({digits[1:4]}) {digits[4:7]}-{digits[7:]}"
    return raw

def now_str() -> str:
    months = ["Jan","Feb","Mar","Apr","May","Jun",
              "Jul","Aug","Sep","Oct","Nov","Dec"]
    d = datetime.now()
    return f"{months[d.month-1]} {d.day}"

# ── Core scraper ──────────────────────────────────────────────────────────────

def scrape_google_maps(query: str, limit: int, headless: bool = True) -> list:
    leads = []

    with sync_playwright() as p:
        browser = p.chromium.launch(
            headless=headless,
            args=["--no-sandbox", "--disable-blink-features=AutomationControlled"]
        )
        context = browser.new_context(
            user_agent=(
                "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/120.0.0.0 Safari/537.36"
            ),
            viewport={"width": 1280, "height": 800},
            locale="en-US"
        )
        page = context.new_page()

        # ── Navigate to Google Maps search ──────────────────────────────────
        search_url = f"https://www.google.com/maps/search/{query.replace(' ', '+')}"
        print(f"\n🌐 Opening: {search_url}")
        page.goto(search_url, wait_until="domcontentloaded", timeout=30000)
        human_delay(2, 4)

        # Accept cookies if prompted (EU)
        try:
            accept_btn = page.locator('button:has-text("Accept all"), button:has-text("Reject all")').first
            if accept_btn.is_visible(timeout=3000):
                accept_btn.click()
                human_delay()
        except Exception:
            pass

        # ── Collect listing links by scrolling the results panel ────────────
        print(f"📜 Scrolling results to collect up to {limit} listings...")
        listing_urls = []
        seen = set()

        results_panel = page.locator('div[role="feed"]')

        scroll_attempts = 0
        max_scrolls = max(30, limit // 3)

        while len(listing_urls) < limit and scroll_attempts < max_scrolls:
            # Grab all currently visible result links
            links = page.locator('a[href*="/maps/place/"]').all()
            for link in links:
                href = link.get_attribute("href") or ""
                # Normalise to the place URL
                match = re.search(r'(/maps/place/[^?]+)', href)
                if match:
                    place_path = match.group(1)
                    if place_path not in seen:
                        seen.add(place_path)
                        full_url = "https://www.google.com" + href
                        listing_urls.append(full_url)

            if len(listing_urls) >= limit:
                break

            # Scroll the panel down
            try:
                results_panel.evaluate("el => el.scrollBy(0, 800)")
            except Exception:
                page.keyboard.press("End")

            human_delay(1.0, 2.5)
            scroll_attempts += 1

            # Check for "You've reached the end of the list"
            end_text = page.locator('text="You\'ve reached the end of the list"')
            if end_text.is_visible(timeout=500):
                print("   ↳ Reached end of results")
                break

        listing_urls = listing_urls[:limit]
        print(f"   ↳ Found {len(listing_urls)} listing URLs")

        # ── Visit each listing and extract details ───────────────────────────
        for i, url in enumerate(listing_urls):
            print(f"   [{i+1}/{len(listing_urls)}] ", end="", flush=True)
            try:
                lead = extract_listing(page, url)
                if lead:
                    leads.append(lead)
                    flag = "✓ LEAD" if not lead["has_website"] else "  skip (has website)"
                    print(f"{lead['name'][:35]:<35} score={lead['score']:>3}  {flag}")
                else:
                    print("(skipped)")
            except Exception as e:
                print(f"Error: {e}")

            human_delay(1.5, 3.5)

        browser.close()

    qualifying = [l for l in leads if not l["has_website"]]
    print(f"\n✅ Scraped {len(leads)} listings → {len(qualifying)} without websites")
    return qualifying


def extract_listing(page, url: str) -> dict | None:
    """Navigate to a single Google Maps listing and extract all data."""
    page.goto(url, wait_until="domcontentloaded", timeout=20000)
    human_delay(1.2, 2.5)

    # Wait for the business name to load
    try:
        page.wait_for_selector('h1', timeout=8000)
    except PlaywrightTimeout:
        return None

    # ── Name ────────────────────────────────────────────────────────────────
    name = ""
    try:
        name = page.locator('h1').first.inner_text(timeout=3000).strip()
    except Exception:
        return None
    if not name:
        return None

    # ── Website ─────────────────────────────────────────────────────────────
    has_website = False
    website_url = ""
    try:
        # Website button has aria-label containing "website" or data-tooltip
        web_btn = page.locator(
            'a[data-item-id="authority"], '
            'a[aria-label*="website" i], '
            'a[href*="http"]:not([href*="google"]):not([href*="goo.gl"])'
        ).first
        if web_btn.is_visible(timeout=2000):
            href = web_btn.get_attribute("href") or ""
            if href.startswith("http") and "google.com" not in href:
                has_website = True
                website_url = href
    except Exception:
        pass

    # ── Phone ────────────────────────────────────────────────────────────────
    phone_raw = ""
    try:
        phone_el = page.locator(
            '[data-item-id*="phone"], '
            'button[aria-label*="phone" i], '
            'span[aria-label*="Phone" i]'
        ).first
        if phone_el.is_visible(timeout=2000):
            phone_raw = phone_el.get_attribute("aria-label") or phone_el.inner_text(timeout=1000)
            phone_raw = re.sub(r'(?i)phone[:\s]*', '', phone_raw).strip()
    except Exception:
        pass

    # Fallback: look for phone pattern in page text
    if not phone_raw:
        try:
            content = page.content()
            match = re.search(r'\(?\d{3}\)?[\s\-\.]\d{3}[\s\-\.]\d{4}', content)
            if match:
                phone_raw = match.group(0)
        except Exception:
            pass

    # ── Category / Type ──────────────────────────────────────────────────────
    biz_type = ""
    try:
        # The category pill below the name
        type_el = page.locator('button[jsaction*="category"], span.DkEaL').first
        if type_el.is_visible(timeout=2000):
            biz_type = type_el.inner_text(timeout=1000).strip()
    except Exception:
        pass

    # ── Address ──────────────────────────────────────────────────────────────
    address = ""
    try:
        addr_el = page.locator(
            '[data-item-id="address"], '
            'button[aria-label*="Address" i], '
            'span[aria-label*="Address" i]'
        ).first
        if addr_el.is_visible(timeout=2000):
            address = addr_el.get_attribute("aria-label") or addr_el.inner_text(timeout=1000)
            address = re.sub(r'(?i)address[:\s]*', '', address).strip()
    except Exception:
        pass

    # ── Reviews ──────────────────────────────────────────────────────────────
    review_count = 0
    try:
        review_el = page.locator('span[aria-label*="review" i]').first
        if review_el.is_visible(timeout=2000):
            label = review_el.get_attribute("aria-label") or review_el.inner_text(timeout=1000)
            review_count = parse_review_count(label)
    except Exception:
        pass

    # ── Photos ───────────────────────────────────────────────────────────────
    has_photos = False
    try:
        photo_el = page.locator('img[src*="lh5.googleusercontent"], button[aria-label*="photo" i]').first
        has_photos = photo_el.is_visible(timeout=1500)
    except Exception:
        pass

    # ── Build lead dict ──────────────────────────────────────────────────────
    score   = score_lead(has_website, review_count, has_photos)
    missing = detect_missing(has_website, review_count, has_photos)

    return {
        "id":          int(time.time() * 1000) + random.randint(0, 999),
        "name":        name,
        "type":        biz_type or "Business",
        "phone":       fmt_phone(phone_raw),
        "raw_phone":   phone_raw,
        "area":        address,
        "score":       score,
        "status":      "new",
        "missing":     missing,
        "notes":       [],
        "mapsUrl":     url,
        "has_website": has_website,
        "website_url": website_url,
        "reviews":     review_count,
        "scraped":     now_str(),
    }

# ── Output ────────────────────────────────────────────────────────────────────

def write_csv(leads: list):
    if not leads:
        return
    fields = ["name","type","phone","area","score","status","missing","reviews","scraped","mapsUrl"]
    with open(CSV_OUTPUT, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=fields, extrasaction="ignore")
        w.writeheader()
        for l in leads:
            row = l.copy()
            row["missing"] = ", ".join(l.get("missing", []))
            w.writerow(row)
    print(f"✅ CSV → {CSV_OUTPUT} ({len(leads)} leads)")

def write_crm_json(leads: list):
    existing = []
    if os.path.exists(CRM_JSON_PATH):
        with open(CRM_JSON_PATH, "r") as f:
            try:
                existing = json.load(f)
            except Exception:
                existing = []

    existing_phones = {l.get("raw_phone","") or l.get("phone","") for l in existing}
    new_leads = [l for l in leads if (l.get("raw_phone") or l.get("phone","")) not in existing_phones]
    merged = new_leads + existing
    with open(CRM_JSON_PATH, "w") as f:
        json.dump(merged, f, indent=2)
    print(f"✅ CRM JSON → {CRM_JSON_PATH} (+{len(new_leads)} new, {len(existing)} existing)")

# ── CLI ───────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="LocalReach Free Scraper")
    parser.add_argument("--query",    required=True,  help='e.g. "restaurants in Austin TX"')
    parser.add_argument("--limit",    type=int, default=40, help="Max listings (default: 40)")
    parser.add_argument("--headless", action="store_true",  help="Run browser in background (no window)")
    parser.add_argument("--output",   choices=["csv","json","both"], default="both")
    args = parser.parse_args()

    leads = scrape_google_maps(args.query, args.limit, headless=args.headless)

    if not leads:
        print("No qualifying leads found. Try a different query.")
        exit(0)

    # Summary table
    print(f"\n{'─'*62}")
    print(f"{'Business':<32} {'Score':>5}  {'Missing'}")
    print(f"{'─'*62}")
    for l in sorted(leads, key=lambda x: -x["score"])[:20]:
        m = ", ".join(l.get("missing", []))
        print(f"{l['name'][:31]:<32} {l['score']:>5}  {m}")
    print(f"{'─'*62}\n")

    if args.output in ("csv",  "both"): write_csv(leads)
    if args.output in ("json", "both"): write_crm_json(leads)
