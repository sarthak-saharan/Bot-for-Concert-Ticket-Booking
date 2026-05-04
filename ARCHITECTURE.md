# Concert Ticket Price Monitor — System Architecture

## Overview

A Python-based bot that polls concert ticket platforms on a schedule, filters listings by your preferred sections and price range, and fires notifications (email or Telegram) the moment a matching ticket appears.

---

## High-Level Architecture

```
┌─────────────────────────────────────────────────────────────┐
│                        CLI / Config                          │
│  config.yaml  ──►  add alert, remove alert, run, status     │
└───────────────────────────┬─────────────────────────────────┘
                            │
                            ▼
┌─────────────────────────────────────────────────────────────┐
│                     Scheduler (APScheduler)                  │
│  Polls every N minutes → triggers Monitor Runner            │
└───────────────────────────┬─────────────────────────────────┘
                            │
              ┌─────────────┼─────────────┐
              ▼             ▼             ▼
      ┌──────────────┐ ┌──────────┐ ┌──────────────┐
      │  SeatGeek    │ │ StubHub  │ │  Vivid Seats │
      │  API Client  │ │ Scraper  │ │  Scraper     │
      └──────┬───────┘ └────┬─────┘ └──────┬───────┘
             └──────────────┼──────────────┘
                            │  Raw Listings
                            ▼
              ┌─────────────────────────────┐
              │       Filter Engine          │
              │  • Section match             │
              │  • Price in [min, max]       │
              │  • Deduplicate               │
              │  • New vs. already alerted   │
              └─────────────┬───────────────┘
                            │  Qualified Listings
                            ▼
              ┌─────────────────────────────┐
              │     Price History DB         │
              │       (SQLite)               │
              │  • Log every poll result     │
              │  • Detect price trends       │
              └─────────────┬───────────────┘
                            │
                            ▼
              ┌─────────────────────────────┐
              │    Notification Dispatcher   │
              │  • Email (SMTP / SendGrid)   │
              │  • Telegram Bot              │
              │  • (Twilio SMS — optional)   │
              └─────────────────────────────┘
```

---

## Tech Stack

| Layer              | Choice                          | Why                                      |
|--------------------|---------------------------------|------------------------------------------|
| Language           | Python 3.11+                    | Rich ecosystem, fast iteration           |
| Scheduling         | APScheduler                     | In-process, no Redis needed for MVP      |
| HTTP               | httpx (async)                   | Async requests, retries, timeouts        |
| HTML Parsing       | BeautifulSoup4 + selectolax     | Fast scraping for fallback scrapers      |
| Database           | SQLite (via sqlite3 / peewee)   | Zero-config, file-based, free            |
| Notifications      | smtplib (email) + python-telegram-bot | Free tiers, easy setup             |
| Config             | YAML (PyYAML)                   | Human-editable config file              |
| CLI                | Typer                           | Clean, typed CLI with minimal boilerplate|
| Retry / Rate limit | tenacity                        | Exponential backoff out of the box       |

---

## Data Models

### Event
```python
Event(
  id: str,               # platform-specific ID
  name: str,             # "Taylor Swift - Eras Tour"
  venue: str,
  city: str,
  date: datetime,
  platform: str,         # "seatgeek" | "stubhub" | "vividseats"
  url: str
)
```

### TicketListing
```python
TicketListing(
  id: str,               # unique listing ID (platform:listing_id)
  event_id: str,
  section: str,          # "Floor A" | "Section 101" | "GA Pit"
  row: str | None,
  quantity: int,
  price_each: float,     # price per ticket (fees included where available)
  price_total: float,
  platform: str,
  url: str,
  fetched_at: datetime
)
```

### UserAlert
```python
UserAlert(
  id: str,
  event_name: str,       # fuzzy-matched search term
  event_date: str | None,
  sections: list[str],   # ["Floor", "Pit", "101", "102"]
  min_price: float,
  max_price: float,
  max_quantity_needed: int,
  platforms: list[str],
  notify_email: str | None,
  notify_telegram_chat_id: str | None,
  active: bool,
  last_alerted: dict     # listing_id → datetime (suppress re-alerts for 1hr)
)
```

### PriceHistory (SQLite table)
```
listing_id | event_id | section | price_each | quantity | platform | recorded_at
```

---

## Filtering Logic

```
for each raw listing:
  1. Normalize section name (lowercase, strip whitespace)
  2. Check if any user section keyword in listing.section  →  keep/skip
  3. Check listing.price_each in [alert.min_price, alert.max_price]  →  keep/skip
  4. Check listing.quantity >= alert.max_quantity_needed  →  keep/skip
  5. Check listing.id NOT in alert.last_alerted (or >1hr ago)  →  keep/skip
  6. Fire notification + update last_alerted
```

---

## Polling Strategy

- **Default interval:** every 15 minutes (configurable per alert)
- **SeatGeek:** official REST API — safe at 15-min intervals
- **StubHub / Vivid Seats:** HTTP scrape with randomized 10–30s delay between pages, rotated User-Agent headers
- **Backoff:** if 429 or 503 → exponential backoff (tenacity), max 3 retries
- **Per-platform circuit breaker:** if 5 consecutive failures → pause platform for 1 hour, log warning

---

## Notification Triggers

```
Trigger when:
  • New listing matches section + price criteria
  • Previously seen listing drops INTO the price range (price drop alert)

Suppress when:
  • Same listing alerted in the last 60 minutes
  • Price moved out of range (no "price went back up" spam)
```

---

## MVP vs V2

### MVP (this implementation)
- Single user, YAML config
- SeatGeek API + StubHub/Vivid scraping stubs
- Email + Telegram notifications
- SQLite history
- CLI to manage alerts

### V2 (future)
- Web dashboard (FastAPI + HTMX)
- Multi-user support with per-user alerts
- Price trend charts (matplotlib / Plotly)
- ML price prediction (simple regression on 7-day history)
- Auto-buy integration (Selenium + manual CAPTCHA solving prompt)
- Redis + Celery for distributed polling at scale

---

## Risks & Limitations

| Risk                        | Mitigation                                               |
|-----------------------------|----------------------------------------------------------|
| Anti-bot detection          | Randomized delays, rotated UA, respect robots.txt        |
| ToS violations              | SeatGeek API is legitimate; scraping is gray area — use responsibly |
| Ticket fees not shown       | Track "price_each" and note it may exclude buyer fees    |
| Platform downtime           | Circuit breaker + silent retry; alert user if >2hr outage|
| Duplicate listings          | Deduplicate by (platform + listing_id)                   |
| Fake tickets                | Out of scope; always buy from reputable sellers          |
| Price spike confusion       | Only alert on ENTER range events, not exit events        |
