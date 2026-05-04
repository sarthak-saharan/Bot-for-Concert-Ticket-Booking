"""
platforms/stubhub.py — StubHub scraper using Playwright for JS-rendered listings.

StubHub renders all ticket listings client-side via JavaScript, so a plain
httpx request only returns a shell HTML page with no ticket data.
Playwright (headless Chromium) is used to fully render the page.

Install: pip install playwright && playwright install chromium
"""

from __future__ import annotations

import logging
import re
from datetime import datetime
from typing import Optional

from models import Event, TicketListing, UserAlert
from platforms.base import BasePlatformClient

logger = logging.getLogger(__name__)

STUBHUB_BASE = "https://www.stubhub.com"


class StubHubClient(BasePlatformClient):
    """Playwright-based scraper for StubHub concert listings."""

    name = "stubhub"

    def __init__(self, user_agent: str = "") -> None:
        self._ua = user_agent or (
            "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/124.0.0.0 Safari/537.36"
        )

    # ------------------------------------------------------------------
    # Public interface
    # ------------------------------------------------------------------

    def search_events(self, alert: UserAlert) -> list[Event]:
        # Priority 1: per-section filtered URLs (most precise)
        section_urls = getattr(alert, "stubhub_section_urls", [])
        if section_urls:
            events = []
            for url in section_urls:
                m = re.search(r"/event/(\d+)", url)
                event_id = m.group(1) if m else "unknown"
                sec_m = re.search(r"sections=(\d+)", url)
                sec_id = sec_m.group(1) if sec_m else "unknown"
                events.append(Event(
                    id=f"stubhub:{event_id}:sec{sec_id}",
                    name=alert.event_name,
                    venue="", city="",
                    date=datetime.utcnow(),
                    platform="stubhub",
                    url=url,
                ))
            logger.info("[stubhub] Using %d section URL(s)", len(events))
            return events

        # Priority 2: generic direct event URL
        if getattr(alert, "stubhub_event_url", None):
            url = alert.stubhub_event_url.split("?")[0].rstrip("/") + "/?quantity=0"
            m = re.search(r"/event/(\d+)", url)
            event_id = m.group(1) if m else "unknown"
            logger.info("[stubhub] Using direct event URL: %s", url)
            return [Event(
                id=f"stubhub:{event_id}",
                name=alert.event_name,
                venue="", city="",
                date=datetime.utcnow(),
                platform="stubhub",
                url=url,
            )]

        # Fallback: search
        query = alert.event_name.replace(" ", "+")
        search_url = f"{STUBHUB_BASE}/search?q={query}"
        logger.info("[stubhub] Searching: %s", search_url)
        try:
            return self._search_with_playwright(search_url)
        except Exception as exc:
            logger.error("[stubhub] search_events failed: %s", exc)
            return []

    def get_listings(self, event: Event) -> list[TicketListing]:
        try:
            listings = self._listings_with_playwright(event.url, event)
            logger.info("[stubhub] Fetched %d listings for '%s'", len(listings), event.name)
            return listings
        except Exception as exc:
            logger.error("[stubhub] get_listings(%s) failed: %s", event.id, exc)
            return []

    # ------------------------------------------------------------------
    # Playwright helpers
    # ------------------------------------------------------------------

    def _get_playwright_page(self, pw):
        """Create a new browser context with anti-bot settings."""
        browser = pw.chromium.launch(
            headless=True,
            args=["--disable-blink-features=AutomationControlled"],
        )
        ctx = browser.new_context(
            user_agent=self._ua,
            viewport={"width": 1280, "height": 800},
            locale="en-US",
        )
        return browser, ctx.new_page()

    def _search_with_playwright(self, url: str) -> list[Event]:
        try:
            from playwright.sync_api import sync_playwright
        except ImportError:
            logger.warning("[stubhub] playwright not installed. Run: pip install playwright && playwright install chromium")
            return []

        events = []
        with sync_playwright() as pw:
            browser, page = self._get_playwright_page(pw)
            try:
                page.goto(url, wait_until="domcontentloaded", timeout=30000)
                page.wait_for_timeout(2500)

                # Extract event links from search results
                links = page.evaluate("""() => {
                    return Array.from(document.querySelectorAll('a[href*="/event/"]'))
                        .map(a => ({href: a.href, text: (a.innerText || '').trim().substring(0, 100)}))
                        .filter(l => l.href && l.text && l.text.length > 3)
                        .slice(0, 5);
                }""")

                for link in links:
                    href = link.get("href", "")
                    m = re.search(r"/event/(\d+)", href)
                    if not m:
                        continue
                    event_id = m.group(1)
                    events.append(Event(
                        id=f"stubhub:{event_id}",
                        name=link.get("text", "Unknown")[:100],
                        venue="",
                        city="",
                        date=datetime.utcnow(),
                        platform="stubhub",
                        url=href.split("?")[0],
                    ))
            finally:
                browser.close()

        return events[:5]

    def _listings_with_playwright(self, url: str, event: Event) -> list[TicketListing]:
        try:
            from playwright.sync_api import sync_playwright
        except ImportError:
            logger.warning("[stubhub] playwright not installed. Run: pip install playwright && playwright install chromium")
            return []

        listings = []
        with sync_playwright() as pw:
            browser, page = self._get_playwright_page(pw)
            try:
                page.goto(url, wait_until="domcontentloaded", timeout=30000)
                page.wait_for_timeout(3500)

                # Scroll both the window and the listings panel to load all lazy-loaded listings.
                # StubHub uses a virtualized list inside a scrollable container — we must scroll
                # that container directly, not just the window.
                for _ in range(25):
                    page.evaluate("""() => {
                        // Try scrolling any tall scrollable container (the listings panel)
                        const scrollables = Array.from(document.querySelectorAll('*')).filter(el => {
                            const s = el.scrollHeight - el.clientHeight;
                            return s > 200 && getComputedStyle(el).overflowY !== 'visible';
                        });
                        scrollables.forEach(el => el.scrollBy(0, 500));
                        window.scrollBy(0, 500);
                    }""")
                    page.wait_for_timeout(400)

                # Extract listing card texts from DOM.
                # Each card's text follows: [Sponsored|]Section X|Row Y|N ticket(s)|...|$PRICE|incl. fees
                cards = page.evaluate("""() => {
                    const results = [];
                    for (const el of document.querySelectorAll('*')) {
                        const txt = (el.innerText || '').trim();
                        if (!/Section\\s+\\S+/.test(txt) || !/\\$[\\d,]+/.test(txt)) continue;
                        if (txt.length < 10 || txt.length > 400) continue;
                        const parentTxt = (el.parentElement?.innerText || '').trim();
                        const parentHasBoth = /Section\\s+\\S+/.test(parentTxt) && /\\$[\\d,]+/.test(parentTxt);
                        if (!parentHasBoth || parentTxt.length > txt.length * 1.8) {
                            results.push(txt.replace(/\\n+/g, '|'));
                        }
                    }
                    return [...new Set(results)];
                }""")

                for card_text in cards:
                    listing = self._parse_card_text(card_text, event)
                    if listing:
                        listings.append(listing)
            finally:
                browser.close()

        return listings

    # ------------------------------------------------------------------
    # Text parsers
    # ------------------------------------------------------------------

    def _parse_card_text(self, text: str, event: Event) -> Optional[TicketListing]:
        """Parse a listing card's pipe-delimited text into a TicketListing."""
        # Strip sponsored label
        text = re.sub(r"^Sponsored\|?", "", text).strip("|")

        price_match = re.search(r"\$([\d,]+)", text)
        if not price_match:
            return None
        price = float(price_match.group(1).replace(",", ""))
        if price <= 0:
            return None

        section_match = re.search(r"(Section\s+[^\s|]+|Floor[^|]{0,20}|Pit[^|]{0,20}|GA[^|]{0,15})", text, re.IGNORECASE)
        if not section_match:
            return None
        section = section_match.group(1).strip()

        row_match = re.search(r"Row\s+([^\s|]+)", text, re.IGNORECASE)
        row = row_match.group(1) if row_match else None

        # "2 - 4 tickets" → use max (4); "2 tickets" → 2
        qty_range = re.search(r"(\d+)\s*[-–]\s*(\d+)\s+ticket", text, re.IGNORECASE)
        qty_single = re.search(r"(\d+)\s+ticket", text, re.IGNORECASE)
        if qty_range:
            quantity = int(qty_range.group(2))
        elif qty_single:
            quantity = int(qty_single.group(1))
        else:
            quantity = 1

        listing_key = f"{event.id}:{section}:{row or ''}:{price}"
        return TicketListing(
            id=f"stubhub:{abs(hash(listing_key)) & 0x7FFFFFFF}",
            event_id=event.id,
            section=section,
            row=row,
            quantity=quantity,
            price_each=price,
            price_total=price * quantity,
            platform="stubhub",
            url=event.url,
            fetched_at=datetime.utcnow(),
        )

    def close(self) -> None:
        pass  # Playwright contexts are closed per-call
