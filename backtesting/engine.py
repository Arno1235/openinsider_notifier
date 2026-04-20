"""Daily-bar portfolio simulator for insider-signal backtests."""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import date, timedelta
from typing import Literal

import numpy as np
import pandas as pd

from backtesting import prices

logger = logging.getLogger(__name__)

SizingMode = Literal["equal_weight", "fixed_dollar", "percent_equity"]
FillWhen = Literal["open", "close"]
OverflowMode = Literal["skip", "queue", "replace_oldest"]


@dataclass
class BacktestConfig:
    start: date
    end: date
    initial_capital: float = 100_000.0
    benchmark: str = "SPY"
    # Entry
    entry_delay_days: int = 1
    fill_when: FillWhen = "open"
    slippage_bps: float = 0.0
    commission_bps: float = 0.0
    # Sizing
    sizing_mode: SizingMode = "equal_weight"
    fixed_dollar: float = 10_000.0
    percent_equity: float = 0.1
    max_concurrent_positions: int = 10
    overflow: OverflowMode = "skip"
    # Exits
    holding_days: int = 20
    stop_loss_pct: float | None = None  # e.g. 0.05 for 5%
    take_profit_pct: float | None = None
    trailing_stop_pct: float | None = None
    # Misc
    risk_free_rate: float = 0.0
    allow_short_on_sell_signal: bool = False


@dataclass
class Position:
    ticker: str
    entry_date: pd.Timestamp
    entry_price: float
    shares: float
    direction: int  # +1 long, -1 short
    planned_exit_date: pd.Timestamp
    high_water: float = 0.0  # for trailing stop
    signal_type: str = ""


@dataclass
class Trade:
    ticker: str
    signal_type: str
    direction: int
    entry_date: pd.Timestamp
    entry_price: float
    exit_date: pd.Timestamp
    exit_price: float
    shares: float
    pnl_dollars: float
    pnl_pct: float
    hold_days: int
    exit_reason: str


@dataclass
class BacktestResult:
    equity: pd.Series  # strategy equity curve
    benchmark_equity: pd.Series  # buy-and-hold benchmark normalized to initial_capital
    returns: pd.Series  # daily strategy returns
    benchmark_returns: pd.Series
    trades: pd.DataFrame
    exposure: pd.Series  # fraction of equity invested per day
    cash: pd.Series
    signals_used: pd.DataFrame
    signals_skipped: pd.DataFrame


def _position_size(
    equity: float,
    cfg: BacktestConfig,
    open_positions: int,
) -> float:
    if cfg.sizing_mode == "fixed_dollar":
        return cfg.fixed_dollar
    if cfg.sizing_mode == "percent_equity":
        return equity * cfg.percent_equity
    # equal_weight: split remaining capital evenly across open slots
    slots = max(cfg.max_concurrent_positions - open_positions, 1)
    return equity / cfg.max_concurrent_positions if cfg.max_concurrent_positions > 0 else equity


def _apply_slippage(price: float, direction: int, bps: float, entering: bool) -> float:
    """Worsen the fill by `bps`."""
    factor = bps / 10_000.0
    sign = 1 if (entering and direction > 0) or (not entering and direction < 0) else -1
    return price * (1 + sign * factor)


def _commission(notional: float, bps: float) -> float:
    return abs(notional) * bps / 10_000.0


def run_backtest(
    signals: pd.DataFrame,
    cfg: BacktestConfig,
    progress_cb=None,
) -> BacktestResult:
    """Run the backtest.

    `signals` must have columns: signal_date, ticker, signal_type, and
    may include company_name, executives, n_executives, total_value.
    """
    # --- master calendar from benchmark ---
    cal_start = cfg.start - timedelta(days=7)
    cal_end = cfg.end + timedelta(days=max(cfg.holding_days * 2, 7))
    bench_px = prices.get_prices(cfg.benchmark, cal_start, cal_end)
    if bench_px.empty:
        raise RuntimeError(
            f"No price data for benchmark {cfg.benchmark}; cannot run backtest."
        )
    calendar = bench_px.index
    calendar = calendar[(calendar.date >= cfg.start) & (calendar.date <= cfg.end)]
    if len(calendar) == 0:
        raise RuntimeError("Benchmark calendar is empty for the selected range.")

    # --- prepare signals ---
    sigs = signals.copy()
    if sigs.empty:
        sigs = pd.DataFrame(
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
    else:
        sigs["signal_date"] = pd.to_datetime(sigs["signal_date"])
        sigs = sigs[
            (sigs["signal_date"].dt.date >= cfg.start)
            & (sigs["signal_date"].dt.date <= cfg.end)
        ].sort_values("signal_date")

    # Preload prices for all signal tickers + benchmark
    tickers = sorted(set(sigs["ticker"].dropna().astype(str).tolist()))
    price_map: dict[str, pd.DataFrame] = {cfg.benchmark: bench_px}
    total = len(tickers) or 1
    for i, t in enumerate(tickers):
        df = prices.get_prices(t, cal_start, cal_end)
        if not df.empty:
            price_map[t] = df
        if progress_cb is not None:
            progress_cb(0.5 + 0.3 * ((i + 1) / total), f"Loaded prices for {t}")

    # Index signals by the calendar day we will actually try to enter on
    def _entry_calendar_date(sig_date: pd.Timestamp) -> pd.Timestamp | None:
        target = sig_date + pd.Timedelta(days=cfg.entry_delay_days)
        # find first calendar day >= target
        idx = calendar.searchsorted(target, side="left")
        if idx >= len(calendar):
            return None
        return calendar[idx]

    sigs["entry_date"] = sigs["signal_date"].map(_entry_calendar_date)
    sigs = sigs.dropna(subset=["entry_date"])
    sigs_by_entry: dict[pd.Timestamp, list] = {}
    for _, r in sigs.iterrows():
        sigs_by_entry.setdefault(r["entry_date"], []).append(r)

    # --- simulation loop ---
    cash = cfg.initial_capital
    open_positions: list[Position] = []
    trades: list[Trade] = []
    used_signals: list[dict] = []
    skipped_signals: list[dict] = []

    equity_curve: list[float] = []
    cash_curve: list[float] = []
    exposure_curve: list[float] = []

    for i, day in enumerate(calendar):
        day_px = {t: df.loc[day] if day in df.index else None for t, df in price_map.items()}

        # 1) Process exits first (exits at open based on prior-day conditions)
        still_open: list[Position] = []
        for pos in open_positions:
            bar = day_px.get(pos.ticker)
            if bar is None:
                # No price today; keep position
                still_open.append(pos)
                continue
            o, h, l, c = float(bar["Open"]), float(bar["High"]), float(bar["Low"]), float(bar["Close"])
            exit_price: float | None = None
            exit_reason: str | None = None

            # Stop-loss / take-profit intraday checks (long-only wording; generalized for short)
            sl_px = None
            tp_px = None
            trail_px = None
            if cfg.stop_loss_pct is not None:
                sl_px = pos.entry_price * (
                    1 - cfg.stop_loss_pct if pos.direction > 0 else 1 + cfg.stop_loss_pct
                )
            if cfg.take_profit_pct is not None:
                tp_px = pos.entry_price * (
                    1 + cfg.take_profit_pct if pos.direction > 0 else 1 - cfg.take_profit_pct
                )
            if cfg.trailing_stop_pct is not None:
                if pos.direction > 0:
                    trail_px = pos.high_water * (1 - cfg.trailing_stop_pct)
                else:
                    trail_px = pos.high_water * (1 + cfg.trailing_stop_pct)

            # Gap-open checks
            if pos.direction > 0:
                if sl_px is not None and o <= sl_px:
                    exit_price, exit_reason = o, "stop_loss"
                elif tp_px is not None and o >= tp_px:
                    exit_price, exit_reason = o, "take_profit"
                elif trail_px is not None and o <= trail_px:
                    exit_price, exit_reason = o, "trailing_stop"
            else:
                if sl_px is not None and o >= sl_px:
                    exit_price, exit_reason = o, "stop_loss"
                elif tp_px is not None and o <= tp_px:
                    exit_price, exit_reason = o, "take_profit"
                elif trail_px is not None and o >= trail_px:
                    exit_price, exit_reason = o, "trailing_stop"

            # Intraday hits (prioritize stop over take-profit conservatively)
            if exit_price is None:
                if pos.direction > 0:
                    if sl_px is not None and l <= sl_px:
                        exit_price, exit_reason = sl_px, "stop_loss"
                    elif trail_px is not None and l <= trail_px:
                        exit_price, exit_reason = trail_px, "trailing_stop"
                    elif tp_px is not None and h >= tp_px:
                        exit_price, exit_reason = tp_px, "take_profit"
                else:
                    if sl_px is not None and h >= sl_px:
                        exit_price, exit_reason = sl_px, "stop_loss"
                    elif trail_px is not None and h >= trail_px:
                        exit_price, exit_reason = trail_px, "trailing_stop"
                    elif tp_px is not None and l <= tp_px:
                        exit_price, exit_reason = tp_px, "take_profit"

            # Holding-period exit at close
            if exit_price is None and day >= pos.planned_exit_date:
                exit_price, exit_reason = c, "holding_period"

            # End-of-backtest
            if exit_price is None and i == len(calendar) - 1:
                exit_price, exit_reason = c, "end_of_backtest"

            if exit_price is None:
                # Update trailing high-water
                if pos.direction > 0:
                    pos.high_water = max(pos.high_water, h)
                else:
                    pos.high_water = min(pos.high_water, l) if pos.high_water else l
                still_open.append(pos)
                continue

            # Apply slippage & commission on exit
            fill = _apply_slippage(exit_price, pos.direction, cfg.slippage_bps, entering=False)
            proceeds = pos.shares * fill * pos.direction
            notional = abs(pos.shares * fill)
            comm = _commission(notional, cfg.commission_bps)
            # Position cash delta:
            # On entry we reduced cash by entry_notional + comm (long) or credited cash (short + margin simplification).
            # Here we close: long -> add proceeds - comm; short -> subtract cost to buyback (-shares*fill) effectively reversed.
            if pos.direction > 0:
                cash += pos.shares * fill
                cash -= comm
                pnl_dollars = pos.shares * (fill - pos.entry_price) - comm
            else:
                # short: profit if fill < entry
                cash -= pos.shares * fill  # shares stored positive
                cash -= comm
                # entry cash effect was: +shares*entry_price (credit). Net pnl:
                pnl_dollars = pos.shares * (pos.entry_price - fill) - comm
            pnl_pct = (fill / pos.entry_price - 1.0) * pos.direction

            trades.append(
                Trade(
                    ticker=pos.ticker,
                    signal_type=pos.signal_type,
                    direction=pos.direction,
                    entry_date=pos.entry_date,
                    entry_price=pos.entry_price,
                    exit_date=day,
                    exit_price=fill,
                    shares=pos.shares,
                    pnl_dollars=pnl_dollars,
                    pnl_pct=pnl_pct,
                    hold_days=int((day - pos.entry_date).days),
                    exit_reason=exit_reason or "unknown",
                )
            )
        open_positions = still_open

        # 2) Enter new positions for today's signals
        day_sigs = sigs_by_entry.get(day, [])
        # Sort by total_value desc (prioritize bigger clusters)
        day_sigs_sorted = sorted(
            day_sigs,
            key=lambda s: float(s.get("total_value", 0) or 0),
            reverse=True,
        )
        for sig in day_sigs_sorted:
            ticker = str(sig["ticker"])
            sig_type = str(sig["signal_type"])
            direction = 1
            if sig_type.endswith("sell"):
                if not cfg.allow_short_on_sell_signal:
                    skipped_signals.append({**sig.to_dict(), "skip_reason": "sell_not_enabled"})
                    continue
                direction = -1

            # Check capacity
            if len(open_positions) >= cfg.max_concurrent_positions:
                if cfg.overflow == "skip":
                    skipped_signals.append({**sig.to_dict(), "skip_reason": "max_positions"})
                    continue
                elif cfg.overflow == "replace_oldest":
                    # close the oldest at today's open
                    open_positions.sort(key=lambda p: p.entry_date)
                    oldest = open_positions.pop(0)
                    bar = day_px.get(oldest.ticker)
                    if bar is not None:
                        fill = _apply_slippage(float(bar["Open"]), oldest.direction, cfg.slippage_bps, entering=False)
                        notional = abs(oldest.shares * fill)
                        comm = _commission(notional, cfg.commission_bps)
                        if oldest.direction > 0:
                            cash += oldest.shares * fill - comm
                            pnl_dollars = oldest.shares * (fill - oldest.entry_price) - comm
                        else:
                            cash -= oldest.shares * fill + comm
                            pnl_dollars = oldest.shares * (oldest.entry_price - fill) - comm
                        trades.append(
                            Trade(
                                ticker=oldest.ticker,
                                signal_type=oldest.signal_type,
                                direction=oldest.direction,
                                entry_date=oldest.entry_date,
                                entry_price=oldest.entry_price,
                                exit_date=day,
                                exit_price=fill,
                                shares=oldest.shares,
                                pnl_dollars=pnl_dollars,
                                pnl_pct=(fill / oldest.entry_price - 1.0) * oldest.direction,
                                hold_days=int((day - oldest.entry_date).days),
                                exit_reason="replaced_by_newer",
                            )
                        )
                # else 'queue' would require bookkeeping; skip for now
                elif cfg.overflow == "queue":
                    skipped_signals.append({**sig.to_dict(), "skip_reason": "queue_not_implemented"})
                    continue

            # Skip duplicates: don't open another position on the same ticker in same direction
            if any(p.ticker == ticker and p.direction == direction for p in open_positions):
                skipped_signals.append({**sig.to_dict(), "skip_reason": "already_open"})
                continue

            bar = day_px.get(ticker)
            if bar is None:
                skipped_signals.append({**sig.to_dict(), "skip_reason": "no_price_data"})
                continue

            fill_raw = float(bar["Open"] if cfg.fill_when == "open" else bar["Close"])
            if not np.isfinite(fill_raw) or fill_raw <= 0:
                skipped_signals.append({**sig.to_dict(), "skip_reason": "bad_price"})
                continue
            fill = _apply_slippage(fill_raw, direction, cfg.slippage_bps, entering=True)

            # Compute current equity (mark-to-market of opens at today's close for sizing? use cash+mtm at day open)
            mtm = 0.0
            for p in open_positions:
                b = day_px.get(p.ticker)
                if b is not None:
                    pr = float(b["Open"] if cfg.fill_when == "open" else b["Close"])
                    mtm += p.shares * pr * p.direction
                else:
                    mtm += p.shares * p.entry_price * p.direction
            equity_now = cash + mtm
            size_dollars = _position_size(equity_now, cfg, len(open_positions))
            if size_dollars <= 0:
                skipped_signals.append({**sig.to_dict(), "skip_reason": "size_zero"})
                continue
            if direction > 0 and size_dollars > cash:
                size_dollars = cash * 0.99
            if size_dollars < fill:
                skipped_signals.append({**sig.to_dict(), "skip_reason": "insufficient_cash"})
                continue

            shares = size_dollars / fill
            notional = shares * fill
            comm = _commission(notional, cfg.commission_bps)

            if direction > 0:
                cash -= notional + comm
            else:
                cash += notional - comm  # short credit (simplified, no borrow fees)

            # planned exit date = entry_date + holding_days trading days
            idx_today = calendar.searchsorted(day)
            planned_exit_idx = min(int(idx_today) + cfg.holding_days, len(calendar) - 1)
            planned_exit = calendar[planned_exit_idx]

            open_positions.append(
                Position(
                    ticker=ticker,
                    entry_date=day,
                    entry_price=fill,
                    shares=shares,
                    direction=direction,
                    planned_exit_date=planned_exit,
                    high_water=fill,
                    signal_type=sig_type,
                )
            )
            used_signals.append({**sig.to_dict(), "entry_fill_price": fill, "shares": shares})

        # 3) Mark-to-market end of day (use Close)
        mtm = 0.0
        for p in open_positions:
            b = day_px.get(p.ticker)
            pr = float(b["Close"]) if b is not None else p.entry_price
            mtm += p.shares * pr * p.direction
        equity = cash + mtm
        equity_curve.append(equity)
        cash_curve.append(cash)
        exposure_curve.append((abs(mtm) / equity) if equity > 0 else 0.0)

        if progress_cb is not None and (i % 10 == 0 or i == len(calendar) - 1):
            progress_cb(0.8 + 0.2 * ((i + 1) / len(calendar)), f"Sim {day.date()}")

    equity_series = pd.Series(equity_curve, index=calendar, name="equity")
    cash_series = pd.Series(cash_curve, index=calendar, name="cash")
    exposure_series = pd.Series(exposure_curve, index=calendar, name="exposure")

    # Benchmark buy-and-hold: buy at first calendar day's Open, mark to Close
    bench_sub = bench_px.loc[calendar]
    bench_entry = float(bench_sub.iloc[0]["Open"])
    bench_shares = cfg.initial_capital / bench_entry
    bench_equity = pd.Series(
        bench_shares * bench_sub["Close"].astype(float).values,
        index=calendar,
        name="benchmark_equity",
    )

    strat_returns = equity_series.pct_change().fillna(0.0)
    bench_returns = bench_equity.pct_change().fillna(0.0)

    trades_df = pd.DataFrame([t.__dict__ for t in trades])
    used_df = pd.DataFrame(used_signals)
    skipped_df = pd.DataFrame(skipped_signals)

    return BacktestResult(
        equity=equity_series,
        benchmark_equity=bench_equity,
        returns=strat_returns,
        benchmark_returns=bench_returns,
        trades=trades_df,
        exposure=exposure_series,
        cash=cash_series,
        signals_used=used_df,
        signals_skipped=skipped_df,
    )
