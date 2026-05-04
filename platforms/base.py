"""
platforms/base.py — Abstract base class for all platform clients.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Optional

from models import Event, TicketListing, UserAlert


class BasePlatformClient(ABC):
    """
    Every platform client must implement:
      - search_events: find events matching the alert's event_name/date
      - get_listings:  fetch ticket listings for a specific event
    """

    name: str = "base"

    @abstractmethod
    def search_events(self, alert: UserAlert) -> list[Event]:
        """Return events that match the alert's search criteria."""
        ...

    @abstractmethod
    def get_listings(self, event: Event) -> list[TicketListing]:
        """Return all available ticket listings for the given event."""
        ...
