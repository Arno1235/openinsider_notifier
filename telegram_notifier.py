"""Send Telegram messages for insider cluster alerts."""

import logging
from datetime import datetime
from typing import Any

import requests

logger = logging.getLogger(__name__)

TELEGRAM_API = "https://api.telegram.org/bot{token}/sendMessage"

TRANSACTION_TYPE_LABELS = {"P": "BUY", "S": "SELL"}


def _format_date(date_str: str) -> str:
    """Parse and format date string for display (YYYY-MM-DD)."""
    if not date_str:
        return "Unknown date"
    for fmt in ("%Y-%m-%d", "%m/%d/%Y", "%d/%m/%Y"):
        try:
            dt = datetime.strptime(date_str.strip(), fmt)
            return dt.strftime("%Y-%m-%d")
        except ValueError:
            continue
    return date_str


def _format_value(value: float) -> str:
    """Format value as currency string."""
    if value >= 1_000_000:
        return f"${value / 1_000_000:.2f}M"
    return f"${value:,.0f}"


def format_alert_message(alert: dict[str, Any]) -> str:
    """Format a single alert as Telegram message text."""
    company = alert.get("company_name", "")
    ticker = alert.get("ticker", "")
    executives = alert.get("executives", [])
    total = alert.get("total_value", 0)

    lines = [
        "🔔 Insider Cluster Alert",
        "",
        f"Company: {company} ({ticker})",
        f"Executives: {len(executives)}",
        "",
    ]
    for e in executives:
        name = e.get("name", "Unknown")
        title = e.get("title", "")
        val = e.get("total_value", 0)
        if title:
            lines.append(f"• {name} ({title}): {_format_value(val)} total")
        else:
            lines.append(f"• {name}: {_format_value(val)} total")
        for tx in e.get("transactions", []):
            date_str = _format_date(tx.get("trade_date", ""))
            tx_type = tx.get("transaction_type", "")
            label = TRANSACTION_TYPE_LABELS.get(tx_type, tx_type or "?")
            tx_val = tx.get("value", 0)
            lines.append(f"  - {date_str}: {label} {_format_value(tx_val)}")
    lines.extend(["", f"Total: {_format_value(total)}"])
    return "\n".join(lines)


def send_telegram(
    bot_token: str,
    chat_id: str,
    text: str,
) -> bool:
    """Send a message via Telegram Bot API. Returns True on success."""
    url = TELEGRAM_API.format(token=bot_token)
    try:
        r = requests.post(
            url,
            json={"chat_id": chat_id, "text": text},
            timeout=10,
        )
        r.raise_for_status()
        return True
    except requests.RequestException as e:
        logger.error("Failed to send Telegram message: %s", e)
        return False


def notify_alerts(
    alerts: list[dict[str, Any]],
    bot_token: str,
    chat_id: str,
) -> int:
    """Send Telegram message for each alert. Returns count of successful sends."""
    sent = 0
    for alert in alerts:
        msg = format_alert_message(alert)
        if send_telegram(bot_token, chat_id, msg):
            sent += 1
    return sent
