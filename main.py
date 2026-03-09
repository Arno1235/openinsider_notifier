"""Scheduler loop: poll openinsider, detect clusters, send Telegram alerts."""

import json
import logging
import os
import time
from datetime import datetime
from pathlib import Path

from analyzer import analyze
from config import (
    DEDUP_STATE_PATH,
    LOOKBACK_DAYS,
    MIN_EXECUTIVES,
    MIN_TRANSACTION_VALUE,
    POLL_INTERVAL_HOURS,
    TELEGRAM_BOT_TOKEN,
    TELEGRAM_CHAT_ID,
)
from scraper import scrape
from telegram_notifier import notify_alerts

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
)
logger = logging.getLogger(__name__)

DEDUP_TTL_HOURS = 24


def _alert_id(alert: dict) -> str:
    """Generate unique ID for deduplication."""
    ticker = alert.get("ticker", "")
    execs = ",".join(sorted(e.get("name", "") for e in alert.get("executives", [])))
    return f"{ticker}|{execs}"


def _load_dedup_state(path: str) -> dict[str, float]:
    """Load dedup state: {alert_id: last_sent_timestamp}."""
    p = Path(path)
    if not p.exists():
        return {}
    try:
        with open(p) as f:
            data = json.load(f)
        return data.get("sent", {})
    except (json.JSONDecodeError, OSError) as e:
        logger.warning("Could not load dedup state: %s", e)
        return {}


def _save_dedup_state(path: str, sent: dict[str, float]) -> None:
    """Save dedup state."""
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    try:
        with open(p, "w") as f:
            json.dump({"sent": sent, "updated": datetime.now().isoformat()}, f)
    except OSError as e:
        logger.warning("Could not save dedup state: %s", e)


def _prune_old_entries(sent: dict[str, float], ttl_hours: float) -> dict[str, float]:
    """Remove entries older than ttl_hours."""
    now = time.time()
    cutoff = now - (ttl_hours * 3600)
    return {k: v for k, v in sent.items() if v > cutoff}


def run_poll() -> None:
    """Execute one poll cycle: scrape, analyze, dedup, notify."""
    logger.info("Starting poll cycle")
    try:
        transactions = scrape(
            lookback_days=LOOKBACK_DAYS,
            min_transaction_value=MIN_TRANSACTION_VALUE,
        )
    except Exception as e:
        logger.error("Scrape failed: %s", e)
        return

    alerts = analyze(
        transactions,
        min_executives=MIN_EXECUTIVES,
        min_transaction_value=MIN_TRANSACTION_VALUE,
    )
    if not alerts:
        logger.info("No cluster alerts this cycle")
        return

    state_path = DEDUP_STATE_PATH
    sent = _load_dedup_state(state_path)
    sent = _prune_old_entries(sent, DEDUP_TTL_HOURS)

    new_alerts = []
    now = time.time()
    for alert in alerts:
        aid = _alert_id(alert)
        if aid in sent:
            logger.debug("Skipping duplicate alert: %s", aid)
            continue
        new_alerts.append(alert)
        sent[aid] = now

    if not new_alerts:
        logger.info("All %d alerts already sent (dedup)", len(alerts))
        return

    count = notify_alerts(new_alerts, TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID)
    logger.info("Sent %d/%d new alerts via Telegram", count, len(new_alerts))
    _save_dedup_state(state_path, sent)


def main() -> None:
    """Run scheduler loop."""
    logger.info(
        "OpenInsider alert bot started: poll every %dh, min %d executives, min $%s",
        POLL_INTERVAL_HOURS,
        MIN_EXECUTIVES,
        f"{MIN_TRANSACTION_VALUE:,}",
    )
    interval_seconds = POLL_INTERVAL_HOURS * 3600
    while True:
        run_poll()
        logger.info("Sleeping %d hours until next poll", POLL_INTERVAL_HOURS)
        time.sleep(interval_seconds)


if __name__ == "__main__":
    main()
