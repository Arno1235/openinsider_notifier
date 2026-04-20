"""Generate entry signals from openinsider historical transactions.

Chunks arbitrary date ranges into monthly windows so openinsider's 5000-row
cap is not hit, caches each chunk to parquet, and derives one or more
signal types from the resulting transactions.
"""

from __future__ import annotations

import logging
import re
from collections import defaultdict
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Iterable

import pandas as pd

from scraper import scrape_range

logger = logging.getLogger(__name__)

CACHE_DIR = Path(".cache/insiders")

PURCHASE = "P"
SALE = "S"

SIGNAL_CLUSTER_BUY = "cluster_buy"
SIGNAL_CLUSTER_SELL = "cluster_sell"
SIGNAL_SINGLE_LARGE_BUY = "single_large_buy"
SIGNAL_CEO_CFO_BUY = "ceo_cfo_buy"

ALL_SIGNAL_TYPES = [
    SIGNAL_CLUSTER_BUY,
    SIGNAL_CLUSTER_SELL,
    SIGNAL_SINGLE_LARGE_BUY,
    SIGNAL_CEO_CFO_BUY,
]

SIGNAL_LABELS = {
    SIGNAL_CLUSTER_BUY: "Cluster Buy (N+ execs)",
    SIGNAL_CLUSTER_SELL: "Cluster Sell (N+ execs)",
    SIGNAL_SINGLE_LARGE_BUY: "Single Large Buy",
    SIGNAL_CEO_CFO_BUY: "CEO/CFO/Chair Buy",
}

TOP_TITLE_RE = re.compile(
    r"\b(CEO|CFO|COO|Chief Executive|Chief Financial|Chair|President|Pres\b)",
    re.IGNORECASE,
)


@dataclass
class SignalConfig:
    start: date
    end: date
    min_transaction_value: float = 200_000
    min_executives: int = 3
    cluster_window_days: int = 30
    single_large_buy_value: float = 1_000_000
    enabled_types: tuple[str, ...] = (SIGNAL_CLUSTER_BUY,)


def _month_ranges(start: date, end: date) -> list[tuple[date, date]]:
    """Split [start, end] into month-ish windows of <= ~31 days each."""
    out: list[tuple[date, date]] = []
    cur = start
    while cur <= end:
        nxt = min(cur + timedelta(days=30), end)
        out.append((cur, nxt))
        cur = nxt + timedelta(days=1)
    return out


def _cache_path(chunk_start: date, chunk_end: date, min_value: int) -> Path:
    name = f"{chunk_start:%Y%m%d}_{chunk_end:%Y%m%d}_{int(min_value)}.parquet"
    return CACHE_DIR / name


def _is_chunk_historical(chunk_end: date) -> bool:
    """Only cache chunks that are fully in the past (>=2 days old)."""
    return chunk_end <= date.today() - timedelta(days=2)


def _parse_trade_date(s: str) -> pd.Timestamp | None:
    if not s:
        return None
    for fmt in ("%Y-%m-%d", "%m/%d/%Y", "%d/%m/%Y"):
        try:
            return pd.Timestamp(datetime.strptime(s.strip(), fmt))
        except ValueError:
            continue
    return None


def fetch_transactions(
    start: date,
    end: date,
    min_transaction_value: int,
    progress_cb=None,
) -> pd.DataFrame:
    """Fetch all insider transactions in [start, end], chunked + cached."""
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    chunks = _month_ranges(start, end)
    frames: list[pd.DataFrame] = []
    total = len(chunks)
    for i, (cs, ce) in enumerate(chunks):
        cache_file = _cache_path(cs, ce, min_transaction_value)
        use_cache = _is_chunk_historical(ce) and cache_file.exists()
        if use_cache:
            df = pd.read_parquet(cache_file)
            logger.info("Loaded %d rows from cache %s", len(df), cache_file.name)
        else:
            records = scrape_range(cs, ce, min_transaction_value)
            df = pd.DataFrame(records)
            if _is_chunk_historical(ce) and not df.empty:
                try:
                    df.to_parquet(cache_file, index=False)
                except Exception as e:
                    logger.warning("Could not cache %s: %s", cache_file, e)
        if not df.empty:
            frames.append(df)
        if progress_cb is not None:
            progress_cb((i + 1) / total, f"Fetched {cs} → {ce}")

    if not frames:
        return pd.DataFrame(
            columns=[
                "filing_date",
                "trade_date",
                "ticker",
                "company_name",
                "owner_name",
                "title",
                "transaction_type",
                "last_price",
                "qty",
                "value",
            ]
        )
    out = pd.concat(frames, ignore_index=True)
    out["trade_date_parsed"] = out["trade_date"].map(_parse_trade_date)
    out = out.dropna(subset=["trade_date_parsed"])
    out = out[(out["trade_date_parsed"].dt.date >= start) & (out["trade_date_parsed"].dt.date <= end)]
    # de-dup identical rows across overlapping chunks (none expected, but safe)
    out = out.drop_duplicates(
        subset=[
            "filing_date",
            "trade_date",
            "ticker",
            "owner_name",
            "transaction_type",
            "value",
        ]
    )
    return out.reset_index(drop=True)


def _derive_cluster_signals(
    tx: pd.DataFrame,
    side: str,
    min_executives: int,
    min_value: float,
    window_days: int,
) -> list[dict]:
    """
    For each ticker, slide over trade dates and emit a signal whenever
    there are at least N unique executives within the trailing `window_days`
    whose individual transaction(s) each sum to >= min_value on that side.
    The signal_date is the date of the Nth executive entering the window.
    Only one signal per rolling cluster (debounced until window clears).
    """
    sub = tx[(tx["transaction_type"] == side) & (tx["value"] >= min_value)].copy()
    if sub.empty:
        return []
    sub = sub.sort_values("trade_date_parsed")

    signals: list[dict] = []
    for ticker, group in sub.groupby("ticker"):
        group = group.sort_values("trade_date_parsed").reset_index(drop=True)
        # Per-executive running values inside the window
        window_start_idx = 0
        last_signal_date: pd.Timestamp | None = None
        for i, row in group.iterrows():
            d = row["trade_date_parsed"]
            # shrink window
            while (
                window_start_idx < i
                and (d - group.loc[window_start_idx, "trade_date_parsed"]).days > window_days
            ):
                window_start_idx += 1
            window = group.iloc[window_start_idx : i + 1]
            exec_values: dict[str, float] = defaultdict(float)
            for _, r in window.iterrows():
                exec_values[r["owner_name"] or "Unknown"] += r["value"]
            qualifying = [n for n, v in exec_values.items() if v >= min_value]
            if len(qualifying) >= min_executives:
                # Debounce: only emit once per window (i.e., don't re-emit
                # until we've seen the window reset)
                if last_signal_date is None or (d - last_signal_date).days > window_days:
                    signals.append(
                        {
                            "signal_date": d,
                            "ticker": ticker,
                            "signal_type": SIGNAL_CLUSTER_BUY
                            if side == PURCHASE
                            else SIGNAL_CLUSTER_SELL,
                            "n_executives": len(qualifying),
                            "total_value": float(sum(exec_values[n] for n in qualifying)),
                            "executives": ", ".join(qualifying),
                            "company_name": row["company_name"],
                        }
                    )
                    last_signal_date = d
    return signals


def _derive_single_large_buy(tx: pd.DataFrame, min_value: float) -> list[dict]:
    sub = tx[(tx["transaction_type"] == PURCHASE) & (tx["value"] >= min_value)]
    out = []
    for _, row in sub.iterrows():
        out.append(
            {
                "signal_date": row["trade_date_parsed"],
                "ticker": row["ticker"],
                "signal_type": SIGNAL_SINGLE_LARGE_BUY,
                "n_executives": 1,
                "total_value": float(row["value"]),
                "executives": row["owner_name"],
                "company_name": row["company_name"],
            }
        )
    return out


def _derive_ceo_cfo_buy(tx: pd.DataFrame, min_value: float) -> list[dict]:
    sub = tx[
        (tx["transaction_type"] == PURCHASE)
        & (tx["value"] >= min_value)
        & (tx["title"].fillna("").str.contains(TOP_TITLE_RE))
    ]
    out = []
    for _, row in sub.iterrows():
        out.append(
            {
                "signal_date": row["trade_date_parsed"],
                "ticker": row["ticker"],
                "signal_type": SIGNAL_CEO_CFO_BUY,
                "n_executives": 1,
                "total_value": float(row["value"]),
                "executives": f"{row['owner_name']} ({row['title']})",
                "company_name": row["company_name"],
            }
        )
    return out


def build_signals(
    tx: pd.DataFrame,
    cfg: SignalConfig,
) -> pd.DataFrame:
    """Turn a transaction dataframe into a dated signal table."""
    if tx.empty:
        return pd.DataFrame(
            columns=[
                "signal_date",
                "ticker",
                "signal_type",
                "n_executives",
                "total_value",
                "executives",
                "company_name",
            ]
        )
    signals: list[dict] = []
    if SIGNAL_CLUSTER_BUY in cfg.enabled_types:
        signals.extend(
            _derive_cluster_signals(
                tx,
                PURCHASE,
                cfg.min_executives,
                cfg.min_transaction_value,
                cfg.cluster_window_days,
            )
        )
    if SIGNAL_CLUSTER_SELL in cfg.enabled_types:
        signals.extend(
            _derive_cluster_signals(
                tx,
                SALE,
                cfg.min_executives,
                cfg.min_transaction_value,
                cfg.cluster_window_days,
            )
        )
    if SIGNAL_SINGLE_LARGE_BUY in cfg.enabled_types:
        signals.extend(_derive_single_large_buy(tx, cfg.single_large_buy_value))
    if SIGNAL_CEO_CFO_BUY in cfg.enabled_types:
        signals.extend(_derive_ceo_cfo_buy(tx, cfg.min_transaction_value))

    if not signals:
        return pd.DataFrame(
            columns=[
                "signal_date",
                "ticker",
                "signal_type",
                "n_executives",
                "total_value",
                "executives",
                "company_name",
            ]
        )

    df = pd.DataFrame(signals)
    df["signal_date"] = pd.to_datetime(df["signal_date"])
    df = df.sort_values(["signal_date", "ticker"]).reset_index(drop=True)
    # De-duplicate (same ticker, same signal_type, same day)
    df = df.drop_duplicates(subset=["signal_date", "ticker", "signal_type"])
    return df
