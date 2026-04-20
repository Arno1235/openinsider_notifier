"""Historical OHLCV fetching via yfinance with on-disk parquet caching."""

from __future__ import annotations

import logging
from datetime import date, timedelta
from pathlib import Path

import pandas as pd

logger = logging.getLogger(__name__)

CACHE_DIR = Path(".cache/prices")
_MEM_CACHE: dict[str, pd.DataFrame] = {}


def _cache_path(ticker: str) -> Path:
    safe = ticker.upper().replace("/", "_")
    return CACHE_DIR / f"{safe}.parquet"


def _normalize(df: pd.DataFrame) -> pd.DataFrame:
    """Return a DataFrame indexed by date with Open/High/Low/Close/Volume columns."""
    if df is None or df.empty:
        return pd.DataFrame()
    if isinstance(df.columns, pd.MultiIndex):
        df = df.copy()
        df.columns = [c[0] if isinstance(c, tuple) else c for c in df.columns]
    keep = [c for c in ["Open", "High", "Low", "Close", "Adj Close", "Volume"] if c in df.columns]
    df = df[keep].copy()
    df.index = pd.to_datetime(df.index).tz_localize(None)
    df = df[~df.index.duplicated(keep="last")]
    return df


def _load_cache(ticker: str) -> pd.DataFrame:
    p = _cache_path(ticker)
    if not p.exists():
        return pd.DataFrame()
    try:
        df = pd.read_parquet(p)
        df.index = pd.to_datetime(df.index)
        return df
    except Exception as e:
        logger.warning("Could not load price cache for %s: %s", ticker, e)
        return pd.DataFrame()


def _save_cache(ticker: str, df: pd.DataFrame) -> None:
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    try:
        df.to_parquet(_cache_path(ticker))
    except Exception as e:
        logger.warning("Could not save price cache for %s: %s", ticker, e)


def _download(ticker: str, start: date, end: date) -> pd.DataFrame:
    """Download from yfinance (lazy import so the module imports without net)."""
    try:
        import yfinance as yf
    except ImportError as e:
        raise ImportError("yfinance is required. Install with: pip install yfinance") from e

    try:
        # end is exclusive in yfinance, so pad by one day
        raw = yf.download(
            ticker,
            start=start.isoformat(),
            end=(end + timedelta(days=1)).isoformat(),
            auto_adjust=False,
            progress=False,
            threads=False,
        )
    except Exception as e:
        logger.warning("yfinance download failed for %s: %s", ticker, e)
        return pd.DataFrame()
    return _normalize(raw)


def get_prices(ticker: str, start: date, end: date) -> pd.DataFrame:
    """Return OHLCV DataFrame for `ticker` covering [start, end] (inclusive).

    Uses an in-memory + on-disk cache. Falls back to empty DataFrame for
    delisted/invalid tickers. Extends the cache if the requested range
    exceeds what is cached.
    """
    ticker = (ticker or "").strip().upper()
    if not ticker:
        return pd.DataFrame()

    cached = _MEM_CACHE.get(ticker)
    if cached is None:
        cached = _load_cache(ticker)
        _MEM_CACHE[ticker] = cached

    need_download = False
    dl_start, dl_end = start, end
    if cached.empty:
        need_download = True
    else:
        cmin = cached.index.min().date()
        cmax = cached.index.max().date()
        if start < cmin:
            need_download = True
            dl_start = start
            dl_end = max(end, cmax)
        if end > cmax:
            need_download = True
            dl_start = min(start, cmin)
            dl_end = end

    if need_download:
        fresh = _download(ticker, dl_start, dl_end)
        if fresh.empty and cached.empty:
            _MEM_CACHE[ticker] = fresh
            return fresh
        if not fresh.empty:
            merged = pd.concat([cached, fresh])
            merged = merged[~merged.index.duplicated(keep="last")].sort_index()
            _MEM_CACHE[ticker] = merged
            _save_cache(ticker, merged)
            cached = merged

    mask = (cached.index.date >= start) & (cached.index.date <= end)
    return cached.loc[mask].copy()


def get_many(
    tickers: list[str],
    start: date,
    end: date,
    progress_cb=None,
) -> dict[str, pd.DataFrame]:
    """Fetch a batch of tickers; returns {ticker: df}. Missing/empty omitted."""
    out: dict[str, pd.DataFrame] = {}
    total = max(len(tickers), 1)
    for i, t in enumerate(tickers):
        df = get_prices(t, start, end)
        if not df.empty:
            out[t] = df
        if progress_cb is not None:
            progress_cb((i + 1) / total, f"Prices: {t}")
    return out


def trading_calendar(benchmark: str, start: date, end: date) -> pd.DatetimeIndex:
    """Use the benchmark's trading days as the master calendar."""
    df = get_prices(benchmark, start, end)
    if df.empty:
        # Fallback to business days
        return pd.bdate_range(start, end)
    return df.index


def clear_cache() -> None:
    """Wipe both in-memory and on-disk price caches."""
    _MEM_CACHE.clear()
    if CACHE_DIR.exists():
        for p in CACHE_DIR.glob("*.parquet"):
            try:
                p.unlink()
            except OSError:
                pass
