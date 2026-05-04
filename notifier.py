"""
notifier.py — Multi-channel notification dispatcher.

Supported channels:
  • Email  — via SMTP (Gmail, SendGrid, etc.)
  • Telegram — via python-telegram-bot (sync wrapper)

Each channel is optional and controlled via config.yaml.
"""

from __future__ import annotations

import logging
import smtplib
import textwrap
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from typing import Optional

from filter_engine import MatchResult

logger = logging.getLogger(__name__)


class _NoMatchResult:
    """Minimal stand-in for MatchResult used only for no-match notifications."""
    def __init__(self, text: str) -> None:
        self._text = text


# ---------------------------------------------------------------------------
# Message formatting
# ---------------------------------------------------------------------------

def _format_subject(result: MatchResult) -> str:
    listing = result.listing
    tag = "🔻 Price Drop" if result.price_dropped else "🎫 Ticket Alert"
    return f"{tag}: {listing.section} @ ${listing.price_each:.0f}/ea — {listing.platform.title()}"


def _format_body(result: MatchResult) -> str:
    listing = result.listing
    alert = result.alert

    reason = "Price dropped into your target range!" if result.price_dropped else "New listing in your target range!"

    return textwrap.dedent(f"""
        🎶 Concert Ticket Price Alert
        ═══════════════════════════════

        Event:     {alert.event_name}
        Platform:  {listing.platform.title()}

        ─── Listing Details ────────────
        Section:   {listing.section}
        Row:       {listing.row or 'N/A'}
        Quantity:  {listing.quantity} ticket(s)
        Price:     ${listing.price_each:.2f} / ticket
        Total:     ${listing.price_total:.2f}

        ─── Your Alert ─────────────────
        Target:    ${alert.min_price:.0f} – ${alert.max_price:.0f}
        Sections:  {', '.join(alert.sections) or 'Any'}

        📌 Reason:  {reason}

        🔗 Link: {listing.url}

        ═══════════════════════════════
        Ticket Bot | Check prices before buying — fees may apply.
    """).strip()


def _format_no_match(alert: "UserAlert", listings: list, elapsed: float) -> str:  # type: ignore
    from models import TicketListing
    sections_str = ", ".join(alert.sections) if alert.sections else "Any"
    msg = (
        f"🔍 *No match found* — {alert.event_name}\n\n"
        f"Checked {len(listings)} listing(s) across all platforms\n"
        f"Target: {sections_str} · ${alert.min_price:.0f}–${alert.max_price:.0f} · qty≥{alert.max_quantity_needed}\n\n"
    )
    if listings:
        # Show the 3 closest listings (cheapest that match section or are nearest in price)
        closest = sorted(listings, key=lambda l: abs(l.price_each - (alert.min_price + alert.max_price) / 2))[:3]
        msg += "*Closest available:*\n"
        for l in closest:
            msg += f"  • {l.section} · ${l.price_each:.0f}/ea · qty {l.quantity} · {l.platform.title()}\n"
    msg += f"\n_Next check in ~{int(elapsed)} min_"
    return msg


def _format_telegram(result: MatchResult) -> str:
    listing = result.listing
    alert = result.alert
    tag = "🔻 *Price Drop*" if result.price_dropped else "🎫 *Ticket Alert*"

    return (
        f"{tag}: *{alert.event_name}*\n\n"
        f"📍 *{listing.section}*{(' | Row ' + listing.row) if listing.row else ''}\n"
        f"💰 *${listing.price_each:.2f}/ea* × {listing.quantity} = ${listing.price_total:.2f}\n"
        f"🏷️ Target: ${alert.min_price:.0f}–${alert.max_price:.0f}\n"
        f"🌐 {listing.platform.title()}\n\n"
        f"[View Tickets]({listing.url})"
    )


# ---------------------------------------------------------------------------
# Email notifier
# ---------------------------------------------------------------------------

class EmailNotifier:
    """Send alerts via SMTP."""

    def __init__(
        self,
        smtp_host: str,
        smtp_port: int,
        username: str,
        password: str,
        from_address: str,
    ) -> None:
        self.smtp_host = smtp_host
        self.smtp_port = smtp_port
        self.username = username
        self.password = password
        self.from_address = from_address or username

    def send(self, to_address: str, result: MatchResult) -> bool:
        """Send a notification email. Returns True on success."""
        subject = _format_subject(result)
        body = _format_body(result)

        msg = MIMEMultipart("alternative")
        msg["Subject"] = subject
        msg["From"] = self.from_address
        msg["To"] = to_address
        msg.attach(MIMEText(body, "plain"))

        # HTML version
        html_body = body.replace("\n", "<br>").replace("═", "─")
        html_body = f"<html><body style='font-family:monospace'>{html_body}</body></html>"
        msg.attach(MIMEText(html_body, "html"))

        try:
            with smtplib.SMTP(self.smtp_host, self.smtp_port) as smtp:
                smtp.ehlo()
                smtp.starttls()
                smtp.login(self.username, self.password)
                smtp.sendmail(self.from_address, to_address, msg.as_string())
            logger.info("[email] Sent alert to %s", to_address)
            return True
        except Exception as exc:
            logger.error("[email] Failed to send to %s: %s", to_address, exc)
            return False


# ---------------------------------------------------------------------------
# Telegram notifier
# ---------------------------------------------------------------------------

class TelegramNotifier:
    """Send alerts via Telegram Bot API (synchronous)."""

    def __init__(self, bot_token: str) -> None:
        self.bot_token = bot_token
        self._base_url = f"https://api.telegram.org/bot{bot_token}"

    def send(self, chat_id: str, result) -> bool:
        """Send a Telegram message. Returns True on success."""
        import httpx

        text = result._text if isinstance(result, _NoMatchResult) else _format_telegram(result)
        try:
            resp = httpx.post(
                f"{self._base_url}/sendMessage",
                json={
                    "chat_id": chat_id,
                    "text": text,
                    "parse_mode": "Markdown",
                    "disable_web_page_preview": False,
                },
                timeout=10,
            )
            resp.raise_for_status()
            logger.info("[telegram] Sent alert to chat_id %s", chat_id)
            return True
        except Exception as exc:
            logger.error("[telegram] Failed to send to %s: %s", chat_id, exc)
            return False

    def test(self, chat_id: str) -> bool:
        """Send a test ping to verify the bot is configured correctly."""
        import httpx
        try:
            resp = httpx.post(
                f"{self._base_url}/sendMessage",
                json={
                    "chat_id": chat_id,
                    "text": "✅ Ticket Bot connected! You'll receive alerts here.",
                },
                timeout=10,
            )
            resp.raise_for_status()
            return True
        except Exception as exc:
            logger.error("[telegram] Test ping failed: %s", exc)
            return False


# ---------------------------------------------------------------------------
# Dispatcher
# ---------------------------------------------------------------------------

class NotificationDispatcher:
    """
    Routes MatchResults to the appropriate notification channel(s)
    based on the alert's configuration.
    """

    def __init__(
        self,
        email_notifier: Optional[EmailNotifier] = None,
        telegram_notifier: Optional[TelegramNotifier] = None,
    ) -> None:
        self.email = email_notifier
        self.telegram = telegram_notifier

    def dispatch(self, result: MatchResult) -> int:
        """
        Send notifications for a match result.
        Returns the number of successful notifications sent.
        """
        alert = result.alert
        sent = 0

        if self.email and alert.notify_email:
            if self.email.send(alert.notify_email, result):
                sent += 1

        if self.telegram and alert.notify_telegram_chat_id:
            if self.telegram.send(alert.notify_telegram_chat_id, result):
                sent += 1

        if sent == 0 and (alert.notify_email or alert.notify_telegram_chat_id):
            logger.warning(
                "[notifier] All channels failed for listing %s", result.listing.id
            )

        return sent

    def dispatch_no_match(self, alert: "UserAlert", listings: list, poll_interval_minutes: int) -> None:  # type: ignore
        """Send a no-match status message to all configured channels for this alert."""
        if not self.telegram or not alert.notify_telegram_chat_id:
            return
        text = _format_no_match(alert, listings, poll_interval_minutes)
        self.telegram.send(alert.notify_telegram_chat_id, _NoMatchResult(text))

    @classmethod
    def from_config(cls, config: "Config") -> "NotificationDispatcher":  # type: ignore
        """Build a dispatcher from the loaded config object."""
        email_notifier = None
        telegram_notifier = None

        ec = config.email_config
        if ec.get("enabled") and ec.get("username") and ec.get("password"):
            email_notifier = EmailNotifier(
                smtp_host=ec["smtp_host"],
                smtp_port=int(ec["smtp_port"]),
                username=ec["username"],
                password=ec["password"],
                from_address=ec.get("from_address", ec["username"]),
            )

        tc = config.telegram_config
        if tc.get("enabled") and tc.get("bot_token"):
            telegram_notifier = TelegramNotifier(bot_token=tc["bot_token"])

        return cls(email_notifier=email_notifier, telegram_notifier=telegram_notifier)
