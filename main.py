#!/usr/bin/env python3
"""
main.py — CLI entry point for the Concert Ticket Price Monitor.

Usage:
    python main.py run              # Start the monitoring scheduler
    python main.py add-alert        # Interactively add a new alert
    python main.py list-alerts      # Show all configured alerts
    python main.py remove-alert ID  # Delete an alert by ID
    python main.py run-once         # Run one monitoring cycle and exit
    python main.py test-notify      # Send a test notification
    python main.py history EVENT_ID # Show price history for an event

Dependencies:
    pip install httpx tenacity beautifulsoup4 selectolax apscheduler pyyaml typer rich
    pip install python-telegram-bot  # for Telegram support
"""

from __future__ import annotations

import logging
import signal
import sys
import time
from pathlib import Path
from typing import Optional

import typer
from rich import print as rprint
from rich.console import Console
from rich.table import Table

from config import Config
from database import PriceHistoryDB
from filter_engine import MatchResult
from models import Event, TicketListing, UserAlert
from monitor import MonitorRunner
from notifier import NotificationDispatcher

app = typer.Typer(help="🎫 Concert Ticket Price Monitor — get notified when prices drop.")
console = Console()


# ---------------------------------------------------------------------------
# Global setup helpers
# ---------------------------------------------------------------------------

def _setup_logging(level: str) -> None:
    logging.basicConfig(
        level=getattr(logging, level.upper(), logging.INFO),
        format="%(asctime)s [%(levelname)s] %(name)s — %(message)s",
        datefmt="%H:%M:%S",
    )


def _load_components(config_path: str = "config.yaml"):
    cfg = Config(Path(config_path))
    _setup_logging(cfg.log_level)
    db = PriceHistoryDB(cfg.db_path)
    dispatcher = NotificationDispatcher.from_config(cfg)
    runner = MonitorRunner(config=cfg, db=db, dispatcher=dispatcher)
    return cfg, db, dispatcher, runner


# ---------------------------------------------------------------------------
# Commands
# ---------------------------------------------------------------------------

@app.command()
def run(
    config: str = typer.Option("config.yaml", "--config", "-c", help="Path to config file"),
    interval: Optional[int] = typer.Option(None, "--interval", "-i", help="Override poll interval (minutes)"),
):
    """Start the monitoring scheduler. Runs indefinitely."""
    from apscheduler.schedulers.blocking import BlockingScheduler

    cfg, db, dispatcher, runner = _load_components(config)
    poll_minutes = interval or cfg.poll_interval_minutes

    rprint(f"\n[bold green]🎫 Ticket Monitor started[/bold green]")
    rprint(f"  Poll interval: [cyan]{poll_minutes} minutes[/cyan]")
    rprint(f"  Config:        [cyan]{config}[/cyan]")
    rprint(f"  Database:      [cyan]{cfg.db_path}[/cyan]")
    rprint(f"\nPress [bold]Ctrl+C[/bold] to stop.\n")

    scheduler = BlockingScheduler(timezone="UTC")
    scheduler.add_job(
        runner.run,
        "interval",
        minutes=poll_minutes,
        id="monitor",
        next_run_time=__import__("datetime").datetime.utcnow(),  # run immediately on start
    )

    def _shutdown(sig, frame):
        rprint("\n[yellow]Shutting down...[/yellow]")
        scheduler.shutdown(wait=False)
        runner.close()
        sys.exit(0)

    signal.signal(signal.SIGINT, _shutdown)
    signal.signal(signal.SIGTERM, _shutdown)

    try:
        scheduler.start()
    except Exception as exc:
        rprint(f"[red]Scheduler error: {exc}[/red]")
        raise typer.Exit(1)


@app.command()
def run_once(
    config: str = typer.Option("config.yaml", "--config", "-c"),
):
    """Run a single monitoring cycle and exit. Good for testing."""
    cfg, db, dispatcher, runner = _load_components(config)
    rprint("[cyan]Running single monitoring cycle...[/cyan]")
    stats = runner.run()
    rprint(f"\n[bold]Results:[/bold]")
    rprint(f"  Listings fetched:    {stats['listings_fetched']}")
    rprint(f"  Notifications sent:  {stats['notifications_sent']}")
    rprint(f"  Elapsed:             {stats.get('elapsed_seconds', 0):.1f}s")
    runner.close()


@app.command(name="add-alert")
def add_alert(
    config: str = typer.Option("config.yaml", "--config", "-c"),
):
    """Interactively add a new price alert."""
    cfg = Config(Path(config))

    rprint("\n[bold cyan]➕ Add New Ticket Alert[/bold cyan]\n")

    event_name = typer.prompt("Event name (e.g. 'Taylor Swift Eras Tour')")
    event_date = typer.prompt("Event date YYYY-MM-DD (leave blank for any)", default="")
    sections_input = typer.prompt(
        "Sections to watch (comma-separated keywords, e.g. 'floor,pit,101')\n"
        "Leave blank to watch ALL sections"
    )
    min_price = typer.prompt("Minimum price per ticket ($)", default=0.0)
    max_price = typer.prompt("Maximum price per ticket ($)", default=300.0)
    quantity = typer.prompt("Minimum tickets needed (contiguous seats)", default=1)

    rprint("\n[bold]Notification channels:[/bold]")
    notify_email = typer.prompt("Email to notify (leave blank to skip)", default="")
    notify_telegram = typer.prompt("Telegram chat ID (leave blank to skip)", default="")

    alert = UserAlert(
        event_name=event_name.strip(),
        event_date=event_date.strip() or None,
        sections=[s.strip().lower() for s in sections_input.split(",") if s.strip()],
        min_price=float(min_price),
        max_price=float(max_price),
        max_quantity_needed=int(quantity),
        notify_email=notify_email.strip() or None,
        notify_telegram_chat_id=notify_telegram.strip() or None,
    )

    cfg.add_alert(alert)
    rprint(f"\n[green]✅ Alert [{alert.id}] saved![/green]")
    rprint(f"   {alert}")


@app.command(name="list-alerts")
def list_alerts(
    config: str = typer.Option("config.yaml", "--config", "-c"),
):
    """List all configured alerts."""
    cfg = Config(Path(config))
    alerts = cfg.get_alerts()

    if not alerts:
        rprint("[yellow]No alerts configured yet. Run [bold]add-alert[/bold] to create one.[/yellow]")
        return

    table = Table(title="📋 Configured Alerts", show_lines=True)
    table.add_column("ID", style="cyan", no_wrap=True)
    table.add_column("Event", style="bold")
    table.add_column("Date")
    table.add_column("Sections")
    table.add_column("Price Range", style="green")
    table.add_column("Qty")
    table.add_column("Active", style="bold")
    table.add_column("Channels")

    for a in alerts:
        channels = []
        if a.notify_email:
            channels.append(f"📧 {a.notify_email}")
        if a.notify_telegram_chat_id:
            channels.append(f"💬 Telegram:{a.notify_telegram_chat_id}")

        table.add_row(
            a.id,
            a.event_name,
            a.event_date or "Any",
            ", ".join(a.sections) or "[dim]Any[/dim]",
            f"${a.min_price:.0f}–${a.max_price:.0f}",
            str(a.max_quantity_needed),
            "✅" if a.active else "⏸️",
            "\n".join(channels) or "[dim]None[/dim]",
        )

    console.print(table)


@app.command(name="remove-alert")
def remove_alert(
    alert_id: str = typer.Argument(..., help="Alert ID to remove"),
    config: str = typer.Option("config.yaml", "--config", "-c"),
):
    """Remove an alert by its ID."""
    cfg = Config(Path(config))
    if cfg.remove_alert(alert_id):
        rprint(f"[green]✅ Alert [{alert_id}] removed.[/green]")
    else:
        rprint(f"[red]❌ Alert [{alert_id}] not found.[/red]")
        raise typer.Exit(1)


@app.command(name="test-notify")
def test_notify(
    config: str = typer.Option("config.yaml", "--config", "-c"),
):
    """Send a test notification to verify your channels are configured."""
    from datetime import datetime
    cfg = Config(Path(config))
    dispatcher = NotificationDispatcher.from_config(cfg)

    # Create a fake match result for testing
    fake_listing = TicketListing(
        id="test:12345",
        event_id="test:event1",
        section="Floor A",
        row="3",
        quantity=2,
        price_each=199.00,
        price_total=398.00,
        platform="seatgeek",
        url="https://seatgeek.com",
        fetched_at=datetime.utcnow(),
    )
    fake_alert = UserAlert(
        id="test",
        event_name="Test Event (Taylor Swift)",
        sections=["floor"],
        min_price=150.0,
        max_price=250.0,
    )
    alerts = cfg.get_alerts()
    if alerts:
        target_alert = alerts[0]
        fake_alert.notify_email = target_alert.notify_email
        fake_alert.notify_telegram_chat_id = target_alert.notify_telegram_chat_id
    else:
        rprint("[yellow]No alerts found. Add an alert first with [bold]add-alert[/bold].[/yellow]")
        return

    from filter_engine import MatchResult
    result = MatchResult(listing=fake_listing, alert=fake_alert, is_new=True, price_dropped=False)
    sent = dispatcher.dispatch(result)

    if sent > 0:
        rprint(f"[green]✅ Test notification sent on {sent} channel(s).[/green]")
    else:
        rprint("[red]❌ No notifications sent. Check your channel config in config.yaml.[/red]")


@app.command()
def history(
    event_id: str = typer.Argument(..., help="Event ID (e.g. seatgeek:12345)"),
    section: str = typer.Option("", "--section", "-s", help="Filter by section keyword"),
    hours: int = typer.Option(24, "--hours", "-h", help="Look back N hours"),
    config: str = typer.Option("config.yaml", "--config", "-c"),
):
    """Show price history for an event."""
    cfg = Config(Path(config))
    db = PriceHistoryDB(cfg.db_path)

    if section:
        data = db.get_price_trend(event_id, section, hours)
        if not data:
            rprint(f"[yellow]No history for event '{event_id}' section '{section}'.[/yellow]")
            return
        table = Table(title=f"📈 Price Trend: {event_id} / {section}")
        table.add_column("Time (UTC)", style="cyan")
        table.add_column("Avg Price", style="green")
        table.add_column("Min Price", style="bold green")
        table.add_column("Listings")
        for row in data:
            table.add_row(
                row["bucket"],
                f"${row['avg_price']:.2f}",
                f"${row['min_price']:.2f}",
                str(row["count"]),
            )
    else:
        data = db.get_cheapest_per_section(event_id)
        if not data:
            rprint(f"[yellow]No recent listings for event '{event_id}'.[/yellow]")
            return
        table = Table(title=f"💰 Cheapest Listings: {event_id}")
        table.add_column("Section")
        table.add_column("Platform", style="cyan")
        table.add_column("Min Price", style="bold green")
        table.add_column("Qty")
        for row in data:
            table.add_row(
                row["section"],
                row["platform"],
                f"${row['min_price']:.2f}",
                str(row["quantity"]),
            )

    console.print(table)


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    app()
