"""
models.py — Core data models for the Concert Ticket Price Monitor.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import datetime
from typing import Optional


# ---------------------------------------------------------------------------
# Domain models (in-memory / passed between layers)
# ---------------------------------------------------------------------------

@dataclass
class Event:
    """Represents a concert event on a ticketing platform."""

    id: str                    # platform-specific event ID
    name: str                  # e.g. "Taylor Swift – Eras Tour"
    venue: str
    city: str
    date: datetime
    platform: str              # "seatgeek" | "stubhub" | "vividseats"
    url: str

    def __str__(self) -> str:
        return f"{self.name} @ {self.venue}, {self.city} on {self.date:%Y-%m-%d}"


@dataclass
class TicketListing:
    """A single ticket listing returned from a platform."""

    id: str                    # globally unique: "{platform}:{raw_listing_id}"
    event_id: str
    section: str               # e.g. "Floor A", "Section 101", "GA Pit"
    row: Optional[str]
    quantity: int              # number of contiguous seats available
    price_each: float          # per-ticket price (may exclude buyer fees)
    price_total: float         # quantity × price_each
    platform: str
    url: str
    fetched_at: datetime = field(default_factory=datetime.utcnow)

    # Derived helpers
    @property
    def section_normalized(self) -> str:
        return self.section.lower().strip()

    def __str__(self) -> str:
        return (
            f"[{self.platform.upper()}] {self.section} | Row {self.row or 'N/A'} | "
            f"Qty {self.quantity} | ${self.price_each:.2f}/ea | {self.url}"
        )


@dataclass
class UserAlert:
    """
    A user-configured watch rule.

    sections:  list of keywords — a listing matches if ANY keyword appears
               in its section name (case-insensitive substring match).
               e.g. ["floor", "pit", "101", "102"]

    last_alerted: maps listing_id → ISO timestamp of last notification.
                  Used to suppress duplicate alerts within the cooldown window.
    """

    id: str = field(default_factory=lambda: str(uuid.uuid4())[:8])
    event_name: str = ""            # fuzzy search term, e.g. "Taylor Swift"
    event_date: Optional[str] = None  # "YYYY-MM-DD" or None for any date
    sections: list[str] = field(default_factory=list)
    min_price: float = 0.0
    max_price: float = 9999.0
    max_quantity_needed: int = 1    # need at least this many contiguous seats
    platforms: list[str] = field(
        default_factory=lambda: ["seatgeek", "stubhub", "vividseats"]
    )
    notify_email: Optional[str] = None
    notify_telegram_chat_id: Optional[str] = None
    active: bool = True
    last_alerted: dict[str, str] = field(default_factory=dict)  # listing_id → ISO ts
    alert_cooldown_minutes: int = 60  # don't re-alert same listing for this long
    stubhub_event_url: Optional[str] = None  # direct event URL, bypasses search
    stubhub_section_urls: list = field(default_factory=list)  # per-section filtered URLs

    # ------------------------------------------------------------------
    # Serialisation helpers (for YAML round-trips)
    # ------------------------------------------------------------------

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "event_name": self.event_name,
            "event_date": self.event_date,
            "sections": self.sections,
            "min_price": self.min_price,
            "max_price": self.max_price,
            "max_quantity_needed": self.max_quantity_needed,
            "platforms": self.platforms,
            "notify_email": self.notify_email,
            "notify_telegram_chat_id": self.notify_telegram_chat_id,
            "active": self.active,
            "last_alerted": self.last_alerted,
            "alert_cooldown_minutes": self.alert_cooldown_minutes,
            "stubhub_event_url": self.stubhub_event_url,
            "stubhub_section_urls": self.stubhub_section_urls,
        }

    @classmethod
    def from_dict(cls, data: dict) -> "UserAlert":
        return cls(
            id=data.get("id", str(uuid.uuid4())[:8]),
            event_name=data.get("event_name", ""),
            event_date=data.get("event_date"),
            sections=data.get("sections", []),
            min_price=float(data.get("min_price", 0)),
            max_price=float(data.get("max_price", 9999)),
            max_quantity_needed=int(data.get("max_quantity_needed", 1)),
            platforms=data.get("platforms", ["seatgeek", "stubhub", "vividseats"]),
            notify_email=data.get("notify_email"),
            notify_telegram_chat_id=data.get("notify_telegram_chat_id"),
            active=bool(data.get("active", True)),
            last_alerted=data.get("last_alerted", {}),
            alert_cooldown_minutes=int(data.get("alert_cooldown_minutes", 60)),
            stubhub_event_url=data.get("stubhub_event_url"),
            stubhub_section_urls=data.get("stubhub_section_urls", []),
        )

    def __str__(self) -> str:
        return (
            f"Alert [{self.id}] '{self.event_name}' | "
            f"Sections: {self.sections} | "
            f"${self.min_price}–${self.max_price} | "
            f"Qty≥{self.max_quantity_needed}"
        )
