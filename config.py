"""Configuration loaded from environment variables."""

import os
from dotenv import load_dotenv

load_dotenv()


def get_env(key: str, default: str | None = None, required: bool = False) -> str | None:
    """Get environment variable, optionally with default or required check."""
    value = os.getenv(key, default)
    if required and not value:
        raise ValueError(f"Required environment variable {key} is not set")
    return value


def get_env_int(key: str, default: int) -> int:
    """Get environment variable as integer."""
    value = os.getenv(key)
    if value is None:
        return default
    try:
        return int(value)
    except ValueError:
        return default


# Polling
POLL_INTERVAL_HOURS = get_env_int("POLL_INTERVAL_HOURS", 4)
LOOKBACK_DAYS = get_env_int("LOOKBACK_DAYS", 7)

# Detection thresholds
MIN_EXECUTIVES = get_env_int("MIN_EXECUTIVES", 3)
MIN_TRANSACTION_VALUE = get_env_int("MIN_TRANSACTION_VALUE", 200000)

# Telegram (required for sending alerts, but only enforced when actually sending)
TELEGRAM_BOT_TOKEN = get_env("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID = get_env("TELEGRAM_CHAT_ID")

# Deduplication state file path
DEDUP_STATE_PATH = os.getenv("DEDUP_STATE_PATH", "/data/alert_state.json")


def require_telegram_credentials() -> tuple[str, str]:
    """Fetch Telegram creds, raising if missing. Call this at notifier run-time."""
    token = TELEGRAM_BOT_TOKEN
    chat_id = TELEGRAM_CHAT_ID
    if not token:
        raise ValueError("Required environment variable TELEGRAM_BOT_TOKEN is not set")
    if not chat_id:
        raise ValueError("Required environment variable TELEGRAM_CHAT_ID is not set")
    return token, chat_id
