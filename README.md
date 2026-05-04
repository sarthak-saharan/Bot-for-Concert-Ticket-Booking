# Concert Ticket Price Monitor Bot

A Python bot that monitors concert ticket prices across **StubHub**, **VividSeats**, and **SeatGeek**. Set your price range, sections, and quantity: get Telegram alerts the moment listings match. Polls every 15 minutes, tracks price history, and suppresses duplicate alerts automatically.

---

## Features

- Monitors StubHub, VividSeats, and SeatGeek simultaneously
- Sends Telegram notifications when a listing enters your target price range
- Supports section-specific filtering (e.g. Floor, Section 1, Pit)
- Sends "no match" status updates so you know the bot is actively checking
- Tracks price history in a local SQLite database
- Cooldown logic prevents duplicate alerts for the same listing

---

## Requirements

- Python 3.10+
- A Telegram bot token (free, from [@BotFather](https://t.me/BotFather))
- Your Telegram chat ID

---
## Full Picture in One Diagram

```
YOU
 │
 │  Edit config.yaml once
 │  (concert, sections, price, Telegram ID)
 │
 ▼
main.py  ──── "run" command ────►  monitor.py
                                       │
                          Every 15 min │
                                       ├──► stubhub.py   → fetches listings
                                       ├──► seatgeek.py  → fetches listings  
                                       └──► vividseats.py → fetches listings
                                                │
                                                ▼
                                        filter_engine.py
                                        (section? price? qty? cooldown?)
                                                │
                                    ┌───────────┴───────────┐
                                    │ MATCH                 │ NO MATCH
                                    ▼                       ▼
                              notifier.py            notifier.py
                           "Found one! Here's      "Nothing yet,
                            the link →"             checking again
                                    │                in 15 min"
                                    ▼
                              YOUR TELEGRAM
                                    +
                              database.py
                           (saves price history)
```

---
## Installation

**1. Clone the repository**

```bash
git clone https://github.com/sarthak-saharan/Bot-for-Concert-Ticket-Booking.git
cd Bot-for-Concert-Ticket-Booking
```

**2. Install dependencies**

```bash
pip install -r requirements.txt
```

**3. Install Playwright's headless browser** (required for StubHub)

```bash
playwright install chromium
```

---

## Configuration

**1. Copy the example config**

```bash
cp config.yaml.example config.yaml
```

**2. Open `config.yaml` and fill in your details**

```yaml
notifications:
  telegram:
    enabled: true
    bot_token: "YOUR_BOT_TOKEN_HERE"   # from @BotFather
```

**3. Add an alert** under the `alerts:` section:

```yaml
alerts:
  - id: my_alert
    event_name: "Taylor Swift"          # used for StubHub/VividSeats search
    event_date: "2026-06-15"            # optional, YYYY-MM-DD format
    sections:
      - "floor"                         # case-insensitive substring match
      - "section 1"
      - "pit"
    min_price: 200.0
    max_price: 500.0
    max_quantity_needed: 2              # only match listings with ≥ this many tickets
    platforms:
      - stubhub
      - vividseats
      - seatgeek
    notify_telegram_chat_id: "YOUR_CHAT_ID_HERE"
    active: true
    alert_cooldown_minutes: 60
    last_alerted: {}
    stubhub_event_url: ""              # optional — see tip below
    stubhub_section_urls: []           # optional — see tip below
```

### How to get your Telegram chat ID

1. Message your bot on Telegram
2. Open this URL in your browser (replace with your token):
   `https://api.telegram.org/botYOUR_TOKEN/getUpdates`
3. Find `"chat": {"id": 123456789}` — that number is your chat ID

---

## Running the Bot

```bash
python main.py run
```

The bot starts immediately, runs one check, then repeats every 15 minutes. Press `Ctrl+C` to stop.

To run it in the background:

```bash
nohup python main.py run > bot.log 2>&1 &
```

---

## StubHub Tips (Important)

StubHub's default listing page only shows ~10 "best value" picks — your target sections may never appear there. For reliable results, use **section-filtered URLs**.

**How to get a section-filtered URL:**

1. Go to the StubHub event page
2. Click your target section on the seating map
3. Copy the URL from your browser — it will contain `sections=XXXXX`
4. Paste it into `stubhub_section_urls` in your config

```yaml
stubhub_event_url: "https://www.stubhub.com/your-event/event/160413015/"
stubhub_section_urls:
  - "https://www.stubhub.com/your-event/event/160413015/?quantity=0&sections=88761"
  - "https://www.stubhub.com/your-event/event/160413015/?quantity=0&sections=92883"
```

When `stubhub_section_urls` is set, the bot scrapes each section URL directly and ignores the generic event page.

---

## SeatGeek Setup (Optional)

SeatGeek requires a free API key. Register at [platform.seatgeek.com](https://platform.seatgeek.com/) and add your credentials:

```yaml
platforms:
  seatgeek:
    enabled: true
    client_id: "YOUR_CLIENT_ID"
    client_secret: "YOUR_CLIENT_SECRET"
```

Leave blank or set `enabled: false` to skip SeatGeek.

---

## Notifications

**Match alert** — sent when a listing meets all your criteria:
```
🎫 Ticket Alert: Taylor Swift
📍 Floor A | Row 3
💰 $320.00/ea × 2 = $640.00
🏷️ Target: $200–$500
🌐 Stubhub
[View Tickets]
```

**No-match update** — sent each cycle when listings exist but none match, so you know the bot is running:
```
🔍 No match found — Taylor Swift
Checked 18 listing(s) across all platforms
Target: floor, section 1 · $200–$500 · qty≥2

Closest available:
  • Floor B · $520/ea · qty 4 · Stubhub
  • Section 101 · $480/ea · qty 1 · Vividseats

Next check in ~15 min
```

---

## Project Structure

```
├── main.py              # CLI entry point
├── monitor.py           # Core polling loop
├── filter_engine.py     # Listing matching logic
├── notifier.py          # Telegram and email notifications
├── models.py            # Data models (Event, TicketListing, UserAlert)
├── config.py            # Config loader
├── database.py          # SQLite price history
├── platforms/
│   ├── stubhub.py       # Playwright-based StubHub scraper
│   ├── vividseats.py    # VividSeats scraper
│   └── seatgeek.py      # SeatGeek API client
├── config.yaml.example  # Template — copy to config.yaml
└── requirements.txt
```

---

## Troubleshooting

**Bot runs but no Telegram messages arrive**
- Verify your bot token and chat ID are correct in `config.yaml`
- Make sure you've sent at least one message to your bot first
- Check `bot.log` for errors

**StubHub returns 0 listings**
- StubHub heavily rate-limits bots. Try adding section-filtered URLs (see StubHub Tips above)
- Make sure Playwright is installed: `playwright install chromium`

**VividSeats shows wrong city**
- VividSeats search returns the nearest upcoming date. Add a `stubhub_event_url` to target the exact show, or set `event_date` to filter by date.

**"No module named playwright"**
```bash
pip install playwright && playwright install chromium
```
