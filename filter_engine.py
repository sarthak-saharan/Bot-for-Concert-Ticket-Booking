"""
filter_engine.py — Filtering and matching logic.

Given a list of raw TicketListings and a UserAlert, returns the subset
of listings that:
  1. Match at least one of the user's preferred sections
  2. Have a price_each within [min_price, max_price]
  3. Offer at least max_quantity_needed contiguous seats
  4. Haven't already triggered a notification within the cooldown window

Also handles:
  - Deduplication across platforms (same row/section/price → keep cheapest)
  - Detection of "just entered range" vs "already known" listings
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta
from typing import NamedTuple

from models import TicketListing, UserAlert

logger = logging.getLogger(__name__)


class MatchResult(NamedTuple):
    listing: TicketListing
    alert: UserAlert
    is_new: bool          # True if not previously alerted
    price_dropped: bool   # True if price was previously above range


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def filter_listings(
    listings: list[TicketListing],
    alert: UserAlert,
    previous_prices: dict[str, float] | None = None,
) -> list[MatchResult]:
    """
    Run all filter steps and return matched listings as MatchResult objects.

    Args:
        listings:        Raw listings from all platforms for this event.
        alert:           The user's watch rule.
        previous_prices: Mapping of listing_id → last seen price_each.
                         Used to detect "entered price range" events.

    Returns:
        List of MatchResult in ascending price order.
    """
    if not alert.active:
        return []

    previous_prices = previous_prices or {}

    # Step 1: Deduplicate across platforms (keep cheapest per section+row)
    deduplicated = _deduplicate(listings)

    matches: list[MatchResult] = []

    for listing in deduplicated:
        # Step 2: Section filter
        if not _matches_section(listing, alert):
            continue

        # Step 3: Price filter
        if not _in_price_range(listing, alert):
            continue

        # Step 4: Quantity filter
        if listing.quantity < alert.max_quantity_needed:
            continue

        # Step 5: Cooldown filter
        is_new = _is_new_alert(listing, alert)

        # Step 6: Price drop detection
        prev = previous_prices.get(listing.id)
        price_dropped = bool(prev and prev > alert.max_price and listing.price_each <= alert.max_price)

        # Only surface: new listings OR price drops into range
        if is_new or price_dropped:
            matches.append(MatchResult(
                listing=listing,
                alert=alert,
                is_new=is_new,
                price_dropped=price_dropped,
            ))
            logger.debug(
                "[filter] Match: %s | $%.2f | new=%s drop=%s",
                listing.section, listing.price_each, is_new, price_dropped,
            )

    # Sort by price ascending
    matches.sort(key=lambda m: m.listing.price_each)
    return matches


# ---------------------------------------------------------------------------
# Filter predicates
# ---------------------------------------------------------------------------

def _matches_section(listing: TicketListing, alert: UserAlert) -> bool:
    """
    Returns True if ANY keyword in alert.sections matches the listing section.

    Uses word-boundary regex so "section 1" does not match "section 118".
    Special case: empty sections list means "match any section".
    """
    import re
    if not alert.sections:
        return True

    normalized = listing.section_normalized
    for keyword in alert.sections:
        pattern = r"\b" + re.escape(keyword.lower().strip()) + r"\b"
        if re.search(pattern, normalized):
            return True
    return False


def _in_price_range(listing: TicketListing, alert: UserAlert) -> bool:
    """Returns True if listing.price_each is within [min_price, max_price]."""
    return alert.min_price <= listing.price_each <= alert.max_price


def _is_new_alert(listing: TicketListing, alert: UserAlert) -> bool:
    """
    Returns True if this listing has never been alerted OR was alerted
    outside the cooldown window.
    """
    last_alerted_str = alert.last_alerted.get(listing.id)
    if not last_alerted_str:
        return True

    try:
        last_alerted_dt = datetime.fromisoformat(last_alerted_str)
    except ValueError:
        return True

    cooldown = timedelta(minutes=alert.alert_cooldown_minutes)
    return datetime.utcnow() - last_alerted_dt > cooldown


# ---------------------------------------------------------------------------
# Deduplication
# ---------------------------------------------------------------------------

def _deduplicate(listings: list[TicketListing]) -> list[TicketListing]:
    """
    Deduplicate listings by (section_normalized, row, quantity).
    When duplicates exist (same seat from multiple platforms), keep the cheapest.
    """
    seen: dict[str, TicketListing] = {}

    for listing in listings:
        key = _dedup_key(listing)
        existing = seen.get(key)
        if existing is None or listing.price_each < existing.price_each:
            seen[key] = listing

    return list(seen.values())


def _dedup_key(listing: TicketListing) -> str:
    """
    Cross-platform deduplication key.
    Same platform listings always get unique IDs (no dedup needed there).
    Cross-platform: same section + row + quantity = likely same seat block.
    """
    row = (listing.row or "").lower().strip()
    return f"{listing.section_normalized}|{row}|{listing.quantity}"


# ---------------------------------------------------------------------------
# Alert state management
# ---------------------------------------------------------------------------

def mark_alerted(listing: TicketListing, alert: UserAlert) -> None:
    """Record that we have alerted the user about this listing."""
    alert.last_alerted[listing.id] = datetime.utcnow().isoformat()


def prune_old_alerts(alert: UserAlert) -> None:
    """
    Remove stale entries from alert.last_alerted to prevent unbounded growth.
    Removes entries older than 24 hours.
    """
    cutoff = datetime.utcnow() - timedelta(hours=24)
    stale_keys = [
        k for k, v in alert.last_alerted.items()
        if datetime.fromisoformat(v) < cutoff
    ]
    for key in stale_keys:
        del alert.last_alerted[key]

    if stale_keys:
        logger.debug("[filter] Pruned %d stale alert records.", len(stale_keys))
