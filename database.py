"""
database.py — SQLite price history store.

Schema:
    price_history(
        id            TEXT PRIMARY KEY,   -- listing.id
        event_id      TEXT,
        platform      TEXT,
        section       TEXT,
        row           TEXT,
        quantity      INTEGER,
        price_each    REAL,
        price_total   REAL,
        url           TEXT,
        recorded_at   TEXT               -- ISO-8601 UTC
    )
"""

from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from typing import Generator

from models import TicketListing


class PriceHistoryDB:
    """Thin wrapper around SQLite for persisting price observations."""

    def __init__(self, db_path: str = "price_history.db") -> None:
        self.db_path = db_path
        self._init_schema()

    # ------------------------------------------------------------------
    # Schema
    # ------------------------------------------------------------------

    def _init_schema(self) -> None:
        with self._conn() as conn:
            conn.execute("""
                CREATE TABLE IF NOT EXISTS price_history (
                    id          TEXT NOT NULL,
                    event_id    TEXT NOT NULL,
                    platform    TEXT NOT NULL,
                    section     TEXT NOT NULL,
                    row         TEXT,
                    quantity    INTEGER NOT NULL,
                    price_each  REAL NOT NULL,
                    price_total REAL NOT NULL,
                    url         TEXT,
                    recorded_at TEXT NOT NULL
                )
            """)
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_ph_event "
                "ON price_history(event_id, recorded_at)"
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_ph_listing "
                "ON price_history(id, recorded_at)"
            )

    # ------------------------------------------------------------------
    # Write
    # ------------------------------------------------------------------

    def record(self, listing: TicketListing) -> None:
        """Append a price observation for a listing."""
        with self._conn() as conn:
            conn.execute(
                """
                INSERT INTO price_history
                  (id, event_id, platform, section, row,
                   quantity, price_each, price_total, url, recorded_at)
                VALUES (?,?,?,?,?,?,?,?,?,?)
                """,
                (
                    listing.id,
                    listing.event_id,
                    listing.platform,
                    listing.section,
                    listing.row,
                    listing.quantity,
                    listing.price_each,
                    listing.price_total,
                    listing.url,
                    listing.fetched_at.isoformat(),
                ),
            )

    def record_many(self, listings: list[TicketListing]) -> None:
        with self._conn() as conn:
            conn.executemany(
                """
                INSERT INTO price_history
                  (id, event_id, platform, section, row,
                   quantity, price_each, price_total, url, recorded_at)
                VALUES (?,?,?,?,?,?,?,?,?,?)
                """,
                [
                    (
                        l.id, l.event_id, l.platform, l.section, l.row,
                        l.quantity, l.price_each, l.price_total, l.url,
                        l.fetched_at.isoformat(),
                    )
                    for l in listings
                ],
            )

    # ------------------------------------------------------------------
    # Read
    # ------------------------------------------------------------------

    def get_price_history(
        self,
        listing_id: str,
        limit: int = 50,
    ) -> list[dict]:
        """Return recent price observations for a specific listing."""
        with self._conn() as conn:
            rows = conn.execute(
                """
                SELECT price_each, quantity, recorded_at
                FROM price_history
                WHERE id = ?
                ORDER BY recorded_at DESC
                LIMIT ?
                """,
                (listing_id, limit),
            ).fetchall()
        return [
            {"price_each": r[0], "quantity": r[1], "recorded_at": r[2]}
            for r in rows
        ]

    def get_cheapest_per_section(self, event_id: str) -> list[dict]:
        """
        Return the cheapest current listing per section for an event.
        'Current' = recorded in the last 60 minutes.
        """
        cutoff = datetime.utcnow().isoformat()[:16]  # minute precision
        with self._conn() as conn:
            rows = conn.execute(
                """
                SELECT section, platform, MIN(price_each) as min_price,
                       quantity, url, MAX(recorded_at)
                FROM price_history
                WHERE event_id = ?
                  AND recorded_at >= datetime(?, '-60 minutes')
                GROUP BY section, platform
                ORDER BY min_price ASC
                """,
                (event_id, cutoff),
            ).fetchall()
        return [
            {
                "section": r[0],
                "platform": r[1],
                "min_price": r[2],
                "quantity": r[3],
                "url": r[4],
            }
            for r in rows
        ]

    def get_price_trend(self, event_id: str, section: str, hours: int = 24) -> list[dict]:
        """Return average price per poll cycle for trend analysis."""
        with self._conn() as conn:
            rows = conn.execute(
                """
                SELECT strftime('%Y-%m-%dT%H:%M', recorded_at) as bucket,
                       AVG(price_each) as avg_price,
                       MIN(price_each) as min_price,
                       COUNT(*) as listing_count
                FROM price_history
                WHERE event_id = ?
                  AND LOWER(section) LIKE ?
                  AND recorded_at >= datetime('now', ?)
                GROUP BY bucket
                ORDER BY bucket ASC
                """,
                (event_id, f"%{section.lower()}%", f"-{hours} hours"),
            ).fetchall()
        return [
            {
                "bucket": r[0],
                "avg_price": round(r[1], 2),
                "min_price": round(r[2], 2),
                "count": r[3],
            }
            for r in rows
        ]

    # ------------------------------------------------------------------
    # Internal
    # ------------------------------------------------------------------

    @contextmanager
    def _conn(self) -> Generator[sqlite3.Connection, None, None]:
        conn = sqlite3.connect(self.db_path, timeout=10)
        conn.row_factory = sqlite3.Row
        try:
            yield conn
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()
