"""
platforms/vividseats.py — Vivid Seats scraper.

VividSeats uses Next.js with SSR: the initial HTML response contains
__NEXT_DATA__ with event and listing data, so plain httpx works.

Listing data lives at:
  __NEXT_DATA__.props.pageProps.initialTopDealListingsData.data.topDeals
  (returns the 5 best-value listings already embedded in the page)

Search results use /search?searchTerm= and event links use /production/{id}.
"""

from __future__ import annotations

import json
import logging
import random
import re
import time
from datetime import datetime
from typing import Optional

import httpx
from tenacity import retry, stop_after_attempt, wait_exponential

from models import Event, TicketListing, UserAlert
from platforms.base import BasePlatformClient

logger = logging.getLogger(__name__)

VIVIDSEATS_BASE = "https://www.vividseats.com"


class VividSeatsClient(BasePlatformClient):
    """HTTP scraper for Vivid Seats (SSR / Next.js)."""

    name = "vividseats"

    def __init__(self, user_agent: str = "") -> None:
        self._ua = user_agent or (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/124.0.0.0 Safari/537.36"
        )
        self._session = httpx.Client(
            timeout=20,
            follow_redirects=True,
            headers={
                "User-Agent": self._ua,
                "Accept-Language": "en-US,en;q=0.9",
                "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
                "Sec-Fetch-Site": "none",
                "Sec-Fetch-Mode": "navigate",
            },
        )

    # ------------------------------------------------------------------
    # Public interface
    # ------------------------------------------------------------------

    def search_events(self, alert: UserAlert) -> list[Event]:
        query = alert.event_name.replace(" ", "%20")
        search_url = f"{VIVIDSEATS_BASE}/search?searchTerm={query}"
        try:
            html = self._fetch_html(search_url)
            return self._parse_search_results(html)
        except Exception as exc:
            logger.error("[vividseats] search_events failed: %s", exc)
            return []

    def get_listings(self, event: Event) -> list[TicketListing]:
        try:
            html = self._fetch_html(event.url)
            listings = self._parse_listings(html, event)
            logger.info("[vividseats] Fetched %d listings for '%s'", len(listings), event.name)
            return listings
        except Exception as exc:
            logger.error("[vividseats] get_listings(%s) failed: %s", event.id, exc)
            return []

    # ------------------------------------------------------------------
    # HTTP
    # ------------------------------------------------------------------

    @retry(
        wait=wait_exponential(multiplier=1, min=3, max=25),
        stop=stop_after_attempt(3),
    )
    def _fetch_html(self, url: str) -> str:
        time.sleep(random.uniform(1.5, 3.5))
        resp = self._session.get(url)
        if resp.status_code in (403, 429):
            logger.warning("[vividseats] %d on %s", resp.status_code, url)
            raise httpx.HTTPStatusError(str(resp.status_code), request=resp.request, response=resp)
        resp.raise_for_status()
        return resp.text

    # ------------------------------------------------------------------
    # Search parsing
    # ------------------------------------------------------------------

    def _parse_search_results(self, html: str) -> list[Event]:
        events: list[Event] = []

        # Try __NEXT_DATA__ first
        nd = self._extract_next_data(html)
        if nd:
            try:
                pp = nd.get("props", {}).get("pageProps", {})
                # Search page may store productions under various keys
                for key in ("productions", "events", "results", "searchResults"):
                    items = pp.get(key, [])
                    if items:
                        for item in items[:5]:
                            ev = self._raw_to_event(item)
                            if ev:
                                events.append(ev)
                        if events:
                            return events
            except Exception:
                pass

        # HTML fallback: event links use /production/ path
        from bs4 import BeautifulSoup
        soup = BeautifulSoup(html, "html.parser")
        for a in soup.select("a[href*='/production/']"):
            href = a.get("href", "")
            if not href or "parking" in href.lower():
                continue
            text = a.get_text(separator=" ", strip=True)
            # Strip day/time prefix like "FRI May 22 8:00pm"
            name = re.sub(r"^(MON|TUE|WED|THU|FRI|SAT|SUN)\s+\w+\s+\d+\s+[\d:apm]+\s*", "", text, flags=re.IGNORECASE)
            name = re.sub(r"\s*(Find Tickets|Find Parking).*$", "", name, flags=re.IGNORECASE).strip()
            if not name or len(name) < 3:
                continue
            full_url = f"{VIVIDSEATS_BASE}{href}" if href.startswith("/") else href
            # Extract production ID from URL
            m = re.search(r"/production/(\d+)", href)
            event_id = m.group(1) if m else href.rstrip("/").split("/")[-1]
            events.append(Event(
                id=f"vividseats:{event_id}",
                name=name[:100],
                venue="",
                city="",
                date=datetime.utcnow(),
                platform="vividseats",
                url=full_url,
            ))

        return events[:5]

    # ------------------------------------------------------------------
    # Listings parsing
    # ------------------------------------------------------------------

    def _parse_listings(self, html: str, event: Event) -> list[TicketListing]:
        listings: list[TicketListing] = []

        # Primary: __NEXT_DATA__ top deals
        nd = self._extract_next_data(html)
        if nd:
            try:
                pp = nd.get("props", {}).get("pageProps", {})
                top_deals = (
                    pp.get("initialTopDealListingsData", {})
                      .get("data", {})
                      .get("topDeals", [])
                )
                for deal in top_deals:
                    listing = self._parse_vs_deal(deal, event)
                    if listing:
                        listings.append(listing)
                if listings:
                    return listings
            except Exception as exc:
                logger.debug("[vividseats] __NEXT_DATA__ listings parse failed: %s", exc)

        # Fallback: BeautifulSoup listing rows
        from bs4 import BeautifulSoup
        soup = BeautifulSoup(html, "html.parser")
        seen: set[str] = set()
        for row in soup.select('[class*="listingRowContainer"], [class*="listingRow"]'):
            text = row.get_text(separator="|", strip=True)
            if text in seen:
                continue
            seen.add(text)
            listing = self._parse_vs_row_text(text, event)
            if listing:
                listings.append(listing)

        return listings

    # ------------------------------------------------------------------
    # Parsers
    # ------------------------------------------------------------------

    def _raw_to_event(self, ev: dict) -> Optional[Event]:
        event_id = str(ev.get("id", ev.get("eventId", ev.get("productionId", ""))))
        if not event_id:
            return None
        name = ev.get("name", ev.get("eventName", ev.get("headlinerName", "Unknown")))
        venue = ev.get("venueName", ev.get("venue", {}).get("name", "") if isinstance(ev.get("venue"), dict) else "")
        city = ev.get("venueCity", ev.get("venue", {}).get("city", "") if isinstance(ev.get("venue"), dict) else "")
        url_path = ev.get("url", f"/productions/{event_id}")
        url = f"{VIVIDSEATS_BASE}{url_path}" if url_path.startswith("/") else url_path
        try:
            raw_date = ev.get("localDate", ev.get("date", ""))
            # Strip timezone designator like [America/New_York]
            raw_date = re.sub(r"\[.*\]", "", raw_date)
            date = datetime.fromisoformat(raw_date.rstrip("Z"))
        except Exception:
            date = datetime.utcnow()
        return Event(
            id=f"vividseats:{event_id}",
            name=str(name),
            venue=str(venue),
            city=str(city),
            date=date,
            platform="vividseats",
            url=url,
        )

    def _parse_vs_deal(self, deal: dict, event: Event) -> Optional[TicketListing]:
        """Parse a topDeals entry: {"section": "Section 111", "row": "G", "price": "149.11"}"""
        try:
            price = float(deal.get("price", 0))
        except (TypeError, ValueError):
            return None
        if price <= 0:
            return None
        section = str(deal.get("section", "General"))
        row = deal.get("row")
        listing_key = f"{event.id}:{section}:{row or ''}:{price}"
        return TicketListing(
            id=f"vividseats:{abs(hash(listing_key)) & 0x7FFFFFFF}",
            event_id=event.id,
            section=section,
            row=str(row) if row else None,
            quantity=1,
            price_each=price,
            price_total=price,
            platform="vividseats",
            url=event.url,
            fetched_at=datetime.utcnow(),
        )

    def _parse_vs_row_text(self, text: str, event: Event) -> Optional[TicketListing]:
        """
        Parse a listing row text like:
        Section 225|Row X | 1–4 tickets|Lowest Price in Section|9.4|Excellent||Fees Incl.||$103| ea
        """
        price_match = re.search(r"\$([\d,]+)", text)
        if not price_match:
            return None
        price = float(price_match.group(1).replace(",", ""))
        if price <= 0:
            return None

        section_match = re.search(
            r"(Section\s+\S+|Floor[^|]{0,20}|Pit[^|]{0,20}|GA[^|]{0,15})",
            text, re.IGNORECASE
        )
        if not section_match:
            return None
        section = section_match.group(1).strip()

        row_match = re.search(r"Row\s+(\w+)", text, re.IGNORECASE)
        row = row_match.group(1) if row_match else None

        # "1–4 tickets" → use max (4); "2 tickets" → 2
        qty_range = re.search(r"(\d+)[–\-](\d+)\s+ticket", text, re.IGNORECASE)
        qty_single = re.search(r"(\d+)\s+ticket", text, re.IGNORECASE)
        if qty_range:
            quantity = int(qty_range.group(2))
        elif qty_single:
            quantity = int(qty_single.group(1))
        else:
            quantity = 1

        listing_key = f"{event.id}:{section}:{row or ''}:{price}"
        return TicketListing(
            id=f"vividseats:{abs(hash(listing_key)) & 0x7FFFFFFF}",
            event_id=event.id,
            section=section,
            row=row,
            quantity=quantity,
            price_each=price,
            price_total=price * quantity,
            platform="vividseats",
            url=event.url,
            fetched_at=datetime.utcnow(),
        )

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _extract_next_data(self, html: str) -> Optional[dict]:
        """Extract and parse the __NEXT_DATA__ JSON blob from a page."""
        match = re.search(
            r'<script\s+id="__NEXT_DATA__"[^>]*>(.*?)</script>',
            html, re.DOTALL
        )
        if not match:
            return None
        try:
            return json.loads(match.group(1))
        except Exception:
            return None

    def close(self) -> None:
        self._session.close()
