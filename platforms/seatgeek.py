"""
platforms/seatgeek.py — SeatGeek API client (official public API).

SeatGeek provides a free, public REST API. Register at:
  https://platform.seatgeek.com/

Set your client_id (and optionally client_secret) in config.yaml:
  platforms:
    seatgeek:
      client_id: "YOUR_CLIENT_ID"
      client_secret: "YOUR_CLIENT_SECRET"

Rate limits: ~1000 req/day on free tier — well within 15-min polling.
"""

from __future__ import annotations

import logging
import time
from datetime import datetime
from typing import Any, Optional

import httpx
from tenacity import (
    retry,
    stop_after_attempt,
    wait_exponential,
    retry_if_exception_type,
)

from models import Event, TicketListing, UserAlert
from platforms.base import BasePlatformClient

logger = logging.getLogger(__name__)

SEATGEEK_API_BASE = "https://api.seatgeek.com/2"


class SeatGeekClient(BasePlatformClient):
    """Client for the SeatGeek public REST API."""

    name = "seatgeek"

    def __init__(self, client_id: str, client_secret: str = "", user_agent: str = "") -> None:
        self.client_id = client_id
        self.client_secret = client_secret
        self._session = httpx.Client(
            timeout=15,
            headers={
                "User-Agent": user_agent or "TicketBot/1.0",
                "Accept": "application/json",
            },
        )

    # ------------------------------------------------------------------
    # Public interface
    # ------------------------------------------------------------------

    def search_events(self, alert: UserAlert) -> list[Event]:
        """Find SeatGeek events matching the alert's criteria."""
        if not self.client_id:
            logger.warning("[seatgeek] No client_id configured — skipping.")
            return []

        params: dict[str, Any] = {
            "q": alert.event_name,
            "per_page": 10,
            "client_id": self.client_id,
            "type": "concert",
        }
        if alert.event_date:
            # SeatGeek: datetime_local.gte / datetime_local.lte
            params["datetime_local.gte"] = f"{alert.event_date}T00:00:00"
            params["datetime_local.lte"] = f"{alert.event_date}T23:59:59"

        try:
            data = self._get("/events", params)
        except Exception as exc:
            logger.error("[seatgeek] search_events failed: %s", exc)
            return []

        events = []
        for ev in data.get("events", []):
            try:
                events.append(self._parse_event(ev))
            except Exception as exc:
                logger.debug("[seatgeek] Could not parse event: %s", exc)
        return events

    def get_listings(self, event: Event) -> list[TicketListing]:
        """
        Fetch ticket listings for a SeatGeek event.

        SeatGeek's public API returns listings through the /events/{id}/listings
        endpoint (available with a client_id).
        """
        if not self.client_id:
            return []

        # SeatGeek event IDs are integers
        sg_event_id = event.id.replace("seatgeek:", "")
        params = {
            "client_id": self.client_id,
            "per_page": 100,
        }

        try:
            data = self._get(f"/listings", {**params, "event_id": sg_event_id})
        except Exception as exc:
            logger.error("[seatgeek] get_listings(%s) failed: %s", event.id, exc)
            return []

        listings = []
        for raw in data.get("listings", []):
            try:
                listing = self._parse_listing(raw, event)
                if listing:
                    listings.append(listing)
            except Exception as exc:
                logger.debug("[seatgeek] Could not parse listing: %s", exc)

        logger.info("[seatgeek] Fetched %d listings for '%s'", len(listings), event.name)
        return listings

    # ------------------------------------------------------------------
    # Private helpers
    # ------------------------------------------------------------------

    @retry(
        retry=retry_if_exception_type(httpx.HTTPStatusError),
        wait=wait_exponential(multiplier=1, min=2, max=30),
        stop=stop_after_attempt(3),
    )
    def _get(self, path: str, params: dict) -> dict:
        url = f"{SEATGEEK_API_BASE}{path}"
        resp = self._session.get(url, params=params)
        if resp.status_code == 429:
            retry_after = int(resp.headers.get("Retry-After", "30"))
            logger.warning("[seatgeek] Rate limited. Sleeping %ds.", retry_after)
            time.sleep(retry_after)
        resp.raise_for_status()
        return resp.json()

    def _parse_event(self, ev: dict) -> Event:
        venue = ev.get("venue", {})
        return Event(
            id=f"seatgeek:{ev['id']}",
            name=ev.get("title", "Unknown"),
            venue=venue.get("name", ""),
            city=venue.get("city", ""),
            date=datetime.fromisoformat(ev["datetime_local"]),
            platform="seatgeek",
            url=ev.get("url", ""),
        )

    def _parse_listing(self, raw: dict, event: Event) -> Optional[TicketListing]:
        listing_id = str(raw.get("id", ""))
        if not listing_id:
            return None

        # SeatGeek listing price structure
        # raw["stats"]["lowest_price"] for event-level; per-listing: raw["price"]
        price_each = float(raw.get("price", 0))
        if price_each <= 0:
            return None

        quantity = int(raw.get("quantity", 1))
        section = raw.get("section", "General")
        row = raw.get("row") or None

        return TicketListing(
            id=f"seatgeek:{listing_id}",
            event_id=event.id,
            section=section,
            row=row,
            quantity=quantity,
            price_each=price_each,
            price_total=price_each * quantity,
            platform="seatgeek",
            url=raw.get("url", event.url),
            fetched_at=datetime.utcnow(),
        )

    def close(self) -> None:
        self._session.close()
