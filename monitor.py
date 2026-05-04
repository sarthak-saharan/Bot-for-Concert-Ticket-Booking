"""
monitor.py — Core monitoring runner.

The MonitorRunner is invoked on each scheduled tick. It:
  1. Loads active alerts from config
  2. For each alert × platform: searches for events, fetches listings
  3. Runs listings through the filter engine
  4. Fires notifications for any matches
  5. Updates alert state (last_alerted) and persists it
  6. Records all listings to price history DB
"""

from __future__ import annotations

import logging
from datetime import datetime
from typing import Optional

from config import Config
from database import PriceHistoryDB
from filter_engine import MatchResult, filter_listings, mark_alerted, prune_old_alerts
from models import Event, TicketListing, UserAlert
from notifier import NotificationDispatcher
from platforms.base import BasePlatformClient
from platforms.seatgeek import SeatGeekClient
from platforms.stubhub import StubHubClient
from platforms.vividseats import VividSeatsClient

logger = logging.getLogger(__name__)


class MonitorRunner:
    """Executes one full monitoring cycle."""

    def __init__(
        self,
        config: Config,
        db: PriceHistoryDB,
        dispatcher: NotificationDispatcher,
    ) -> None:
        self.config = config
        self.db = db
        self.dispatcher = dispatcher
        self._clients: list[BasePlatformClient] = self._build_clients()

    # ------------------------------------------------------------------
    # Main entry point
    # ------------------------------------------------------------------

    def run(self) -> dict:
        """
        Execute one monitoring cycle across all alerts and platforms.

        Returns a summary dict with stats for logging/debugging.
        """
        start = datetime.utcnow()
        alerts = self.config.get_alerts()
        active_alerts = [a for a in alerts if a.active]

        if not active_alerts:
            logger.info("[monitor] No active alerts configured.")
            return {"alerts": 0, "listings_fetched": 0, "notifications_sent": 0}

        logger.info("[monitor] Running cycle: %d active alert(s).", len(active_alerts))

        total_listings = 0
        total_notifications = 0

        for alert in active_alerts:
            listings, notifications = self._process_alert(alert)
            total_listings += listings
            total_notifications += notifications

        elapsed = (datetime.utcnow() - start).total_seconds()
        logger.info(
            "[monitor] Cycle done in %.1fs | %d listings fetched | %d notifications sent.",
            elapsed, total_listings, total_notifications,
        )
        return {
            "alerts": len(active_alerts),
            "listings_fetched": total_listings,
            "notifications_sent": total_notifications,
            "elapsed_seconds": elapsed,
        }

    # ------------------------------------------------------------------
    # Per-alert processing
    # ------------------------------------------------------------------

    def _process_alert(self, alert: UserAlert) -> tuple[int, int]:
        """Process a single alert. Returns (listings_fetched, notifications_sent)."""
        logger.info("[monitor] Processing alert [%s]: '%s'", alert.id, alert.event_name)

        # Collect listings from all enabled platforms for this alert
        all_listings: list[TicketListing] = []
        for client in self._clients:
            if client.name not in alert.platforms:
                continue
            if not self.config.platform_enabled(client.name):
                continue

            platform_listings = self._fetch_from_platform(client, alert)
            all_listings.extend(platform_listings)

        if not all_listings:
            logger.debug("[monitor] No listings found for alert [%s].", alert.id)
            return 0, 0

        # Build previous price map from DB (last seen prices for this alert's events)
        previous_prices = self._build_previous_prices(all_listings)

        # Run filter engine
        prune_old_alerts(alert)
        matches = filter_listings(all_listings, alert, previous_prices)

        # Record all listings to history DB
        self.db.record_many(all_listings)

        # Send notifications and update alert state
        notifications_sent = 0
        for match in matches:
            n = self.dispatcher.dispatch(match)
            notifications_sent += n
            if n > 0:
                mark_alerted(match.listing, alert)

        # Persist updated last_alerted state
        if matches:
            self.config.update_alert(alert)

        # Send no-match status if we found listings but nothing met the criteria
        if all_listings and not matches:
            self.dispatcher.dispatch_no_match(
                alert, all_listings, self.config.poll_interval_minutes
            )

        logger.info(
            "[monitor] Alert [%s]: %d listings, %d matches, %d notifications.",
            alert.id, len(all_listings), len(matches), notifications_sent,
        )
        return len(all_listings), notifications_sent

    # ------------------------------------------------------------------
    # Platform fetching
    # ------------------------------------------------------------------

    def _fetch_from_platform(
        self, client: BasePlatformClient, alert: UserAlert
    ) -> list[TicketListing]:
        """Search for events on a platform, then fetch their listings."""
        listings: list[TicketListing] = []

        try:
            events = client.search_events(alert)
            logger.debug(
                "[monitor] [%s] Found %d event(s) for '%s'.",
                client.name, len(events), alert.event_name,
            )

            # Filter events by date if specified
            if alert.event_date:
                events = [
                    e for e in events
                    if e.date.strftime("%Y-%m-%d") == alert.event_date
                ]

            for event in events:
                try:
                    event_listings = client.get_listings(event)
                    listings.extend(event_listings)
                except Exception as exc:
                    logger.warning(
                        "[monitor] [%s] get_listings(%s) failed: %s",
                        client.name, event.id, exc,
                    )

        except Exception as exc:
            logger.error(
                "[monitor] [%s] search_events failed for '%s': %s",
                client.name, alert.event_name, exc,
            )

        return listings

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _build_previous_prices(self, listings: list[TicketListing]) -> dict[str, float]:
        """
        Build a map of {listing_id: last_seen_price} from the DB.
        Used by the filter engine to detect "entered range" events.
        """
        prices: dict[str, float] = {}
        for listing in listings:
            history = self.db.get_price_history(listing.id, limit=1)
            if history:
                prices[listing.id] = history[0]["price_each"]
        return prices

    def _build_clients(self) -> list[BasePlatformClient]:
        clients: list[BasePlatformClient] = []
        ua = self.config.user_agent

        if self.config.platform_enabled("seatgeek"):
            cid, csecret = self.config.seatgeek_credentials()
            clients.append(SeatGeekClient(client_id=cid, client_secret=csecret, user_agent=ua))

        if self.config.platform_enabled("stubhub"):
            clients.append(StubHubClient(user_agent=ua))

        if self.config.platform_enabled("vividseats"):
            clients.append(VividSeatsClient(user_agent=ua))

        logger.info("[monitor] Loaded %d platform client(s).", len(clients))
        return clients

    def close(self) -> None:
        """Clean up HTTP sessions."""
        for client in self._clients:
            if hasattr(client, "close"):
                client.close()  # type: ignore
