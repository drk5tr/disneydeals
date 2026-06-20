#!/usr/bin/env python3
"""
disney_offers_monitor.py
Watch the Walt Disney World special-offers page and push a notification when an
offer appears — OR an existing offer's travel window gets extended — so that it
overlaps a target date range (default: Oct 15-30, 2026).

Why not a plain text-diff? Disney constantly rewrites copy and bumps image
timestamps, which would trigger false alerts daily. This script instead parses
each offer's actual travel dates and only alerts on offers that matter to you.

Notifications go through ntfy (free, no account). Designed to run on GitHub
Actions; it persists state.json between runs so it only alerts on real changes.
"""

import datetime as dt
import json
import os
import re
import sys
from pathlib import Path

import requests
from bs4 import BeautifulSoup
from playwright.sync_api import sync_playwright

# ---------------------------------------------------------------------------
# CONFIG — the defaults match your request; override via env vars if needed.
# ---------------------------------------------------------------------------
URL = os.environ.get("MONITOR_URL", "https://disneyworld.disney.go.com/special-offers/")
SITE_ROOT = "https://disneyworld.disney.go.com"

YEAR = int(os.environ.get("MONITOR_YEAR") or "2026")
# Target travel window (inclusive). Format: MM-DD.
WINDOW_START = os.environ.get("MONITOR_WINDOW_START") or "10-15"
WINDOW_END = os.environ.get("MONITOR_WINDOW_END") or "10-30"

# Offers containing any of these words are ignored (case-insensitive).
# Aulani is in Hawaii, not Walt Disney World — drop it by default.
EXCLUDE_KEYWORDS = [
    k.strip().lower()
    for k in os.environ.get("MONITOR_EXCLUDE", "Aulani").split(",")
    if k.strip()
]

# ntfy: install the app, subscribe to this EXACT topic (long & random).
NTFY_SERVER = os.environ.get("NTFY_SERVER", "https://ntfy.sh")
NTFY_TOPIC = os.environ.get("NTFY_TOPIC", "chase-disney-9f3k2p-CHANGE-ME")

STATE_FILE = Path(os.environ.get("MONITOR_STATE_FILE", "state.json"))
TIMEOUT = 30
HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36"
    )
}

# ---------------------------------------------------------------------------
# Date parsing
# ---------------------------------------------------------------------------
_MONTHS = {
    "january": 1, "february": 2, "march": 3, "april": 4, "may": 5, "june": 6,
    "july": 7, "august": 8, "september": 9, "october": 10, "november": 11,
    "december": 12, "jan": 1, "feb": 2, "mar": 3, "apr": 4, "jun": 6, "jul": 7,
    "aug": 8, "sep": 9, "sept": 9, "oct": 10, "nov": 11, "dec": 12,
}
_MONTH_ALT = "|".join(sorted(_MONTHS, key=len, reverse=True))

# "July 30 to October 3, 2026"  /  "January 1, 2026, through December 18, 2026"
_RANGE_RE = re.compile(
    rf"\b(?P<m1>{_MONTH_ALT})\s+(?P<d1>\d{{1,2}})(?:,\s*(?P<y1>\d{{4}}))?\s*"
    rf"(?:to|through|–|-|until)\s*"
    rf"(?P<m2>{_MONTH_ALT})\s+(?P<d2>\d{{1,2}}),?\s*(?P<y2>\d{{4}})",
    re.IGNORECASE,
)
# Single-ended "through December 18, 2026" (no explicit start)
_THROUGH_RE = re.compile(
    rf"\b(?:through|valid through|book through|until)\s+"
    rf"(?P<m>{_MONTH_ALT})\s+(?P<d>\d{{1,2}}),?\s*(?P<y>\d{{4}})",
    re.IGNORECASE,
)


def _d(month_name: str, day: int, year: int):
    try:
        return dt.date(year, _MONTHS[month_name.lower()], day)
    except (KeyError, ValueError):
        return None


def parse_ranges(text: str):
    """Return a list of (start_date, end_date) tuples found in text."""
    ranges = []
    for m in _RANGE_RE.finditer(text):
        y2 = int(m.group("y2"))
        y1 = int(m.group("y1")) if m.group("y1") else y2
        start = _d(m.group("m1"), int(m.group("d1")), y1)
        end = _d(m.group("m2"), int(m.group("d2")), y2)
        if start and end and start <= end:
            ranges.append((start, end))
    if not ranges:
        for m in _THROUGH_RE.finditer(text):
            end = _d(m.group("m"), int(m.group("d")), int(m.group("y")))
            if end:
                ranges.append((dt.date(end.year, 1, 1), end))
    # "travel dates in 2026" / "valid ... in 2026" with no specific range
    if not ranges and re.search(rf"\bin\s+{YEAR}\b", text):
        ranges.append((dt.date(YEAR, 1, 1), dt.date(YEAR, 12, 31)))
    return ranges


def overlaps_window(ranges):
    """True if any range intersects the target window; None if no dates found."""
    if not ranges:
        return None  # unknown -> caller decides (we bias toward alerting)
    ws = _d_from_mmdd(WINDOW_START)
    we = _d_from_mmdd(WINDOW_END)
    return any(s <= we and e >= ws for (s, e) in ranges)


def _d_from_mmdd(mmdd: str):
    mm, dd = mmdd.split("-")
    return dt.date(YEAR, int(mm), int(dd))


# ---------------------------------------------------------------------------
# Offer extraction (class-name independent: keys off the offer detail URL)
# ---------------------------------------------------------------------------
_OFFER_HREF = re.compile(r"/special-offers/([a-z0-9][a-z0-9\-]+)/?$", re.IGNORECASE)


def extract_offers(html: str) -> dict:
    soup = BeautifulSoup(html, "html.parser")
    offers = {}
    for a in soup.find_all("a", href=True):
        href = a["href"].split("?")[0].split("#")[0]
        m = _OFFER_HREF.search(href)
        if not m:
            continue
        slug = m.group(1).lower()

        # Climb to the largest ancestor that still contains only THIS offer's
        # detail link — that isolates the offer card without needing CSS classes.
        chosen = a
        node = a
        for _ in range(8):
            node = node.parent
            if node is None:
                break
            others = 0
            for b in node.find_all("a", href=True):
                mm = _OFFER_HREF.search(b["href"].split("?")[0].split("#")[0])
                if mm and mm.group(1).lower() != slug:
                    others += 1
            if others == 0:
                chosen = node
            else:
                break

        text = re.sub(r"\s+", " ", chosen.get_text(" ", strip=True)).strip()
        heading = chosen.find(["h1", "h2", "h3", "h4"])
        title = heading.get_text(" ", strip=True) if heading else text[:80]
        url = href if href.startswith("http") else SITE_ROOT + href

        # If we've seen this slug before, keep the richer (longer) card text.
        prev = offers.get(slug)
        if prev is None or len(text) > len(prev["text"]):
            offers[slug] = {"slug": slug, "title": title, "text": text, "url": url}
    return offers


def evaluate(offers: dict) -> dict:
    """Annotate each offer with qualifies(bool/None) and a dates summary."""
    out = {}
    for slug, info in offers.items():
        blob = f"{info['title']} {info['text']}".lower()
        if any(k in blob for k in EXCLUDE_KEYWORDS):
            continue  # excluded entirely
        ranges = parse_ranges(info["text"])
        ov = overlaps_window(ranges)
        # Bias toward alerting: unknown dates (None) are treated as qualifying.
        qualifies = True if ov is None else ov
        dates = (
            ", ".join(f"{s.isoformat()}–{e.isoformat()}" for s, e in ranges)
            if ranges else "dates not parsed (verify manually)"
        )
        out[slug] = {**info, "qualifies": qualifies, "dates": dates}
    return out


# ---------------------------------------------------------------------------
# Notify + state
# ---------------------------------------------------------------------------
def notify(title: str, message: str, click: str) -> None:
    requests.post(
        f"{NTFY_SERVER}/{NTFY_TOPIC}",
        data=message.encode("utf-8"),
        headers={"Title": title, "Priority": "high", "Tags": "tada", "Click": click},
        timeout=TIMEOUT,
    )


def load_state() -> dict:
    if STATE_FILE.exists():
        try:
            return json.loads(STATE_FILE.read_text())
        except json.JSONDecodeError:
            return {}
    return {}


def save_state(state: dict) -> None:
    STATE_FILE.write_text(json.dumps(state, indent=2))


_STEALTH_JS = """
// Hide signals Akamai-style bot detection looks for.
Object.defineProperty(navigator, 'webdriver', {get: () => undefined});
Object.defineProperty(navigator, 'languages', {get: () => ['en-US', 'en']});
Object.defineProperty(navigator, 'plugins', {get: () => [1, 2, 3, 4, 5]});
window.chrome = {runtime: {}};
"""


def fetch_rendered_html(url: str) -> str:
    """Render the SPA in headless Chromium and return the DOM after offers load."""
    with sync_playwright() as p:
        # Disney's edge throws HTTP/2 protocol errors at headless Chromium and
        # also fingerprints automation; force HTTP/1.1 and apply basic stealth.
        browser = p.chromium.launch(
            headless=True,
            args=[
                "--disable-http2",
                "--disable-quic",
                "--disable-blink-features=AutomationControlled",
                "--no-sandbox",
            ],
        )
        ctx = browser.new_context(
            user_agent=HEADERS["User-Agent"],
            viewport={"width": 1280, "height": 900},
            locale="en-US",
            extra_http_headers={
                "Accept-Language": "en-US,en;q=0.9",
                "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            },
        )
        ctx.add_init_script(_STEALTH_JS)
        page = ctx.new_page()
        page.goto(url, wait_until="commit", timeout=90_000)
        page.wait_for_selector(
            'a[href*="/special-offers/"][href$="/"]',
            timeout=60_000,
        )
        page.wait_for_timeout(2500)
        html = page.content()
        browser.close()
        return html


def main() -> int:
    baseline = not STATE_FILE.exists()
    prev = load_state()

    try:
        html = fetch_rendered_html(URL)
        offers = evaluate(extract_offers(html))
    except Exception as e:
        print(f"[warn] fetch/parse failed, skipping run: {e}", file=sys.stderr)
        return 0

    if not offers:
        print("[warn] no offers parsed — page layout may have changed.", file=sys.stderr)
        return 0

    # New, or newly-qualifying (window extended into your range), offers.
    fresh = []
    for slug, info in offers.items():
        if not info["qualifies"]:
            continue
        was = prev.get(slug)
        if was is None or not was.get("qualifies"):
            fresh.append(info)

    print(f"[info] {len(offers)} offers; "
          f"{sum(o['qualifies'] for o in offers.values())} overlap "
          f"{WINDOW_START}..{WINDOW_END}/{YEAR}; {len(fresh)} new.")
    for o in fresh:
        print(f"   -> {o['title']}  [{o['dates']}]")

    if baseline:
        print("[info] baseline captured; no alert on first run.")
    elif fresh:
        lines = [f"• {o['title']}\n  {o['dates']}\n  {o['url']}" for o in fresh]
        body = (f"Offer(s) now covering {WINDOW_START}–{WINDOW_END}/{YEAR}:\n\n"
                + "\n\n".join(lines))
        notify(f"Disney deal for your dates ({len(fresh)})", body, fresh[0]["url"])
        print("[info] notified.")
    else:
        print("[info] nothing new for your window.")

    # Persist the full current view so we can detect transitions next time.
    save_state({s: {"qualifies": i["qualifies"], "dates": i["dates"],
                    "title": i["title"]} for s, i in offers.items()})
    return 0


if __name__ == "__main__":
    sys.exit(main())
