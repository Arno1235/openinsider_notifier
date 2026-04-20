"""Streamlit web app for backtesting openinsider signals vs the S&P 500."""

from __future__ import annotations

import logging
from datetime import date, timedelta

import numpy as np
import pandas as pd
import plotly.express as px
import plotly.graph_objects as go
import streamlit as st

from backtesting import prices
from backtesting.engine import BacktestConfig, run_backtest
from backtesting.metrics import (
    drawdown_series,
    monthly_returns,
    summarize,
)
from backtesting.signals import (
    ALL_SIGNAL_TYPES,
    SIGNAL_CEO_CFO_BUY,
    SIGNAL_CLUSTER_BUY,
    SIGNAL_CLUSTER_SELL,
    SIGNAL_LABELS,
    SIGNAL_SINGLE_LARGE_BUY,
    SignalConfig,
    build_signals,
    fetch_transactions,
)

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")

st.set_page_config(
    page_title="OpenInsider Backtest",
    page_icon="",
    layout="wide",
    initial_sidebar_state="expanded",
)


# ---------- Cached data loaders ----------

@st.cache_data(show_spinner=False)
def cached_fetch_transactions(start: date, end: date, min_value: int) -> pd.DataFrame:
    return fetch_transactions(start, end, min_value)


@st.cache_data(show_spinner=False)
def cached_build_signals(
    tx_key: str,  # opaque cache key for the tx frame
    tx: pd.DataFrame,
    cfg_dict: dict,
) -> pd.DataFrame:
    cfg = SignalConfig(
        start=cfg_dict["start"],
        end=cfg_dict["end"],
        min_transaction_value=cfg_dict["min_transaction_value"],
        min_executives=cfg_dict["min_executives"],
        cluster_window_days=cfg_dict["cluster_window_days"],
        single_large_buy_value=cfg_dict["single_large_buy_value"],
        enabled_types=tuple(cfg_dict["enabled_types"]),
    )
    return build_signals(tx, cfg)


def _tx_key(start: date, end: date, min_value: int) -> str:
    return f"{start:%Y%m%d}_{end:%Y%m%d}_{min_value}"


# ---------- Sidebar ----------

with st.sidebar:
    st.title("Backtest settings")

    with st.expander("Date range & capital", expanded=True):
        today = date.today()
        default_start = today - timedelta(days=365 * 3)
        col1, col2 = st.columns(2)
        start_date = col1.date_input("Start", default_start, max_value=today)
        end_date = col2.date_input("End", today, min_value=start_date, max_value=today)
        initial_capital = st.number_input(
            "Initial capital ($)", min_value=1000.0, value=100_000.0, step=10_000.0
        )
        benchmark = st.text_input("Benchmark ticker", value="SPY")
        risk_free_rate = st.number_input(
            "Risk-free rate (annual)", min_value=0.0, max_value=0.2, value=0.02, step=0.005
        )

    with st.expander("Signal filters", expanded=True):
        enabled_types = st.multiselect(
            "Signal types",
            options=ALL_SIGNAL_TYPES,
            default=[SIGNAL_CLUSTER_BUY],
            format_func=lambda s: SIGNAL_LABELS.get(s, s),
        )
        min_executives = st.slider("Min executives (cluster)", 2, 10, 3)
        min_transaction_value = st.number_input(
            "Min transaction value ($)",
            min_value=10_000,
            value=200_000,
            step=50_000,
        )
        cluster_window_days = st.slider(
            "Cluster window (days)", 1, 90, 30,
            help="Rolling window for counting unique executives in a cluster.",
        )
        single_large_buy_value = st.number_input(
            "Single-large-buy threshold ($)",
            min_value=100_000,
            value=1_000_000,
            step=100_000,
        )

    with st.expander("Entry", expanded=False):
        entry_delay_days = st.selectbox(
            "Entry delay (calendar days after signal)", [0, 1, 2, 3], index=1
        )
        fill_when = st.radio("Fill at", ["open", "close"], index=0, horizontal=True)
        slippage_bps = st.number_input("Slippage (bps)", 0.0, 500.0, 5.0, step=1.0)
        commission_bps = st.number_input("Commission (bps per side)", 0.0, 100.0, 1.0, step=0.5)

    with st.expander("Position sizing", expanded=True):
        sizing_mode = st.selectbox(
            "Sizing mode",
            ["equal_weight", "fixed_dollar", "percent_equity"],
            format_func=lambda s: {
                "equal_weight": "Equal weight (capital / max positions)",
                "fixed_dollar": "Fixed $ per trade",
                "percent_equity": "Percent of current equity",
            }[s],
        )
        fixed_dollar = st.number_input(
            "Fixed $ per trade",
            min_value=100.0,
            value=10_000.0,
            step=1_000.0,
            disabled=(sizing_mode != "fixed_dollar"),
        )
        percent_equity = st.slider(
            "Percent of equity per trade",
            0.01, 1.0, 0.10, step=0.01,
            disabled=(sizing_mode != "percent_equity"),
        )
        max_concurrent_positions = st.slider("Max concurrent positions", 1, 50, 10)
        overflow = st.selectbox(
            "When at capacity",
            ["skip", "replace_oldest"],
            format_func=lambda s: {"skip": "Skip new signal", "replace_oldest": "Replace oldest"}[s],
        )

    with st.expander("Exits", expanded=True):
        holding_days = st.slider("Holding period (trading days)", 1, 252, 20)
        use_stop = st.checkbox("Stop-loss", value=False)
        stop_loss_pct = st.slider("Stop-loss %", 1, 50, 10, disabled=not use_stop) / 100.0
        use_tp = st.checkbox("Take-profit", value=False)
        take_profit_pct = st.slider("Take-profit %", 1, 100, 20, disabled=not use_tp) / 100.0
        use_trail = st.checkbox("Trailing stop", value=False)
        trailing_stop_pct = st.slider("Trailing stop %", 1, 50, 15, disabled=not use_trail) / 100.0

    with st.expander("Advanced", expanded=False):
        allow_short = st.checkbox(
            "Short on cluster-sell signals",
            value=False,
            help="Simulated short (no borrow fees modeled).",
        )

    run_col, clear_col = st.columns(2)
    run_clicked = run_col.button("Run backtest", type="primary", use_container_width=True)
    if clear_col.button("Clear cache", use_container_width=True):
        cached_fetch_transactions.clear()
        cached_build_signals.clear()
        prices.clear_cache()
        st.success("Cache cleared")


# ---------- Main ----------

st.title("OpenInsider Backtester")
st.caption(
    "Backtest trading strategies derived from openinsider.com insider-transaction clusters. "
    "All results compare against a buy-and-hold benchmark over the same window."
)

if not run_clicked:
    st.info(
        "Configure settings in the sidebar, then click **Run backtest**. "
        "Historical insider data is cached to `.cache/insiders/` and price data to `.cache/prices/` "
        "to keep subsequent runs fast."
    )
    st.stop()


if not enabled_types:
    st.error("Select at least one signal type in the sidebar.")
    st.stop()

# --- Pipeline ---
progress = st.progress(0.0, text="Starting…")

def _stage(pct: float, msg: str) -> None:
    progress.progress(min(max(pct, 0.0), 1.0), text=msg)

_stage(0.02, "Fetching insider transactions…")
tx = cached_fetch_transactions(start_date, end_date, int(min_transaction_value))
_stage(0.30, f"Fetched {len(tx)} insider transactions")

if tx.empty:
    progress.empty()
    st.error("No insider transactions returned. Try a wider date range or lower min value.")
    st.stop()

_stage(0.35, "Deriving signals…")
signals_cfg = {
    "start": start_date,
    "end": end_date,
    "min_transaction_value": float(min_transaction_value),
    "min_executives": int(min_executives),
    "cluster_window_days": int(cluster_window_days),
    "single_large_buy_value": float(single_large_buy_value),
    "enabled_types": list(enabled_types),
}
signals_df = cached_build_signals(_tx_key(start_date, end_date, int(min_transaction_value)), tx, signals_cfg)
_stage(0.45, f"Built {len(signals_df)} signals")

if signals_df.empty:
    progress.empty()
    st.warning("No signals under those filters. Loosen the thresholds or widen the date range.")
    st.stop()

cfg = BacktestConfig(
    start=start_date,
    end=end_date,
    initial_capital=float(initial_capital),
    benchmark=benchmark.strip().upper() or "SPY",
    entry_delay_days=int(entry_delay_days),
    fill_when=fill_when,
    slippage_bps=float(slippage_bps),
    commission_bps=float(commission_bps),
    sizing_mode=sizing_mode,
    fixed_dollar=float(fixed_dollar),
    percent_equity=float(percent_equity),
    max_concurrent_positions=int(max_concurrent_positions),
    overflow=overflow,
    holding_days=int(holding_days),
    stop_loss_pct=stop_loss_pct if use_stop else None,
    take_profit_pct=take_profit_pct if use_tp else None,
    trailing_stop_pct=trailing_stop_pct if use_trail else None,
    risk_free_rate=float(risk_free_rate),
    allow_short_on_sell_signal=bool(allow_short),
)

_stage(0.50, "Loading price data & simulating…")
try:
    result = run_backtest(signals_df, cfg, progress_cb=_stage)
except Exception as e:
    progress.empty()
    st.exception(e)
    st.stop()
_stage(1.0, "Done")
progress.empty()

stats = summarize(
    result.equity,
    result.returns,
    result.benchmark_equity,
    result.benchmark_returns,
    result.trades,
    result.exposure,
    risk_free_rate=cfg.risk_free_rate,
)


# ---------- Tabs ----------

tabs = st.tabs([
    "Overview",
    "Equity curve",
    "Drawdown",
    "Monthly returns",
    "Trades",
    "Signals",
])

def _fmt_pct(x: float) -> str:
    return f"{x * 100:,.2f}%"


def _fmt_money(x: float) -> str:
    return f"${x:,.0f}"


# Overview
with tabs[0]:
    st.subheader(f"Strategy vs {cfg.benchmark} — {cfg.start} → {cfg.end}")

    def kpi_row(label: str, strat: str, bench: str) -> None:
        c1, c2, c3 = st.columns([2, 2, 2])
        c1.markdown(f"**{label}**")
        c2.metric("Strategy", strat)
        c3.metric(cfg.benchmark, bench)

    kpi_row("Total return", _fmt_pct(stats["total_return"]), _fmt_pct(stats["bench_total_return"]))
    kpi_row("CAGR", _fmt_pct(stats["cagr"]), _fmt_pct(stats["bench_cagr"]))
    kpi_row("Volatility (annualized)", _fmt_pct(stats["volatility"]), _fmt_pct(stats["bench_volatility"]))
    kpi_row("Sharpe", f"{stats['sharpe']:.2f}", f"{stats['bench_sharpe']:.2f}")
    kpi_row("Max drawdown", _fmt_pct(stats["max_drawdown"]), _fmt_pct(stats["bench_max_drawdown"]))

    st.divider()
    c1, c2, c3, c4 = st.columns(4)
    c1.metric("Trades", f"{stats['num_trades']}")
    c2.metric("Win rate", _fmt_pct(stats["win_rate"]))
    c3.metric("Avg trade", _fmt_pct(stats["avg_trade_pct"]))
    c4.metric("Profit factor", f"{stats['profit_factor']:.2f}")

    c5, c6, c7, c8 = st.columns(4)
    c5.metric("Sortino", f"{stats['sortino']:.2f}")
    c6.metric("Calmar", f"{stats['calmar']:.2f}")
    c7.metric("Alpha (annual)", _fmt_pct(stats["alpha"]))
    c8.metric("Beta vs bench", f"{stats['beta']:.2f}")

    c9, c10, c11, c12 = st.columns(4)
    c9.metric("Avg hold (days)", f"{stats['avg_hold_days']:.1f}")
    c10.metric("Exposure", _fmt_pct(stats["exposure"]))
    c11.metric("Best trade", _fmt_pct(stats["best_trade_pct"]))
    c12.metric("Worst trade", _fmt_pct(stats["worst_trade_pct"]))

    st.divider()
    st.markdown("### Ending values")
    c1, c2, c3 = st.columns(3)
    c1.metric("Strategy final equity", _fmt_money(result.equity.iloc[-1]))
    c2.metric(f"{cfg.benchmark} final equity", _fmt_money(result.benchmark_equity.iloc[-1]))
    diff = result.equity.iloc[-1] - result.benchmark_equity.iloc[-1]
    c3.metric("Outperformance ($)", _fmt_money(diff), delta=_fmt_money(diff))


# Equity curve
with tabs[1]:
    st.subheader("Equity curve")
    eq_norm = result.equity / result.equity.iloc[0]
    bench_norm = result.benchmark_equity / result.benchmark_equity.iloc[0]
    fig = go.Figure()
    fig.add_trace(go.Scatter(x=eq_norm.index, y=eq_norm.values, name="Strategy", line=dict(width=2)))
    fig.add_trace(
        go.Scatter(
            x=bench_norm.index, y=bench_norm.values, name=cfg.benchmark, line=dict(width=2, dash="dot")
        )
    )
    fig.update_layout(
        hovermode="x unified",
        yaxis_title="Growth of $1",
        xaxis_title="Date",
        height=500,
        legend=dict(orientation="h", yanchor="bottom", y=1.02, xanchor="right", x=1),
    )
    st.plotly_chart(fig, use_container_width=True)

    st.markdown("### Exposure over time")
    fig2 = go.Figure()
    fig2.add_trace(go.Scatter(x=result.exposure.index, y=result.exposure.values, fill="tozeroy"))
    fig2.update_layout(height=250, yaxis_title="Invested fraction", xaxis_title="Date")
    st.plotly_chart(fig2, use_container_width=True)


# Drawdown
with tabs[2]:
    st.subheader("Underwater plot")
    strat_dd = drawdown_series(result.equity) * 100
    bench_dd = drawdown_series(result.benchmark_equity) * 100
    fig = go.Figure()
    fig.add_trace(go.Scatter(x=strat_dd.index, y=strat_dd.values, name="Strategy", fill="tozeroy"))
    fig.add_trace(
        go.Scatter(
            x=bench_dd.index,
            y=bench_dd.values,
            name=cfg.benchmark,
            line=dict(dash="dot"),
        )
    )
    fig.update_layout(yaxis_title="Drawdown (%)", xaxis_title="Date", height=500, hovermode="x unified")
    st.plotly_chart(fig, use_container_width=True)


# Monthly returns
with tabs[3]:
    st.subheader("Monthly returns (%)")
    c1, c2 = st.columns(2)
    for col, (label, equity_s) in zip(
        (c1, c2),
        (("Strategy", result.equity), (cfg.benchmark, result.benchmark_equity)),
    ):
        pivot = monthly_returns(equity_s)
        if pivot.empty:
            col.info(f"{label}: not enough data")
            continue
        col.markdown(f"**{label}**")
        fig = px.imshow(
            pivot.values,
            x=pivot.columns,
            y=pivot.index,
            color_continuous_scale="RdYlGn",
            color_continuous_midpoint=0,
            aspect="auto",
            text_auto=".1f",
        )
        fig.update_layout(height=400, coloraxis_colorbar_title="%")
        col.plotly_chart(fig, use_container_width=True)


# Trades
with tabs[4]:
    st.subheader(f"Trades ({len(result.trades)})")
    if result.trades.empty:
        st.info("No trades taken.")
    else:
        display = result.trades.copy()
        display["pnl_pct"] = (display["pnl_pct"] * 100).round(2)
        display["pnl_dollars"] = display["pnl_dollars"].round(2)
        display["entry_price"] = display["entry_price"].round(4)
        display["exit_price"] = display["exit_price"].round(4)
        display["shares"] = display["shares"].round(2)
        display["direction"] = display["direction"].map({1: "Long", -1: "Short"})
        display = display.rename(
            columns={
                "pnl_pct": "pnl_%",
                "pnl_dollars": "pnl_$",
            }
        )
        st.dataframe(
            display.sort_values("entry_date", ascending=False),
            use_container_width=True,
            hide_index=True,
        )
        csv = display.to_csv(index=False).encode("utf-8")
        st.download_button("Download trades CSV", csv, "trades.csv", "text/csv")

        st.divider()
        st.markdown("### PnL distribution")
        fig = px.histogram(
            result.trades,
            x=(result.trades["pnl_pct"] * 100),
            nbins=40,
            labels={"x": "Trade return (%)"},
        )
        fig.update_layout(height=350, bargap=0.02)
        st.plotly_chart(fig, use_container_width=True)

        st.markdown("### Exit reasons")
        reason_counts = result.trades["exit_reason"].value_counts().reset_index()
        reason_counts.columns = ["exit_reason", "count"]
        fig = px.bar(reason_counts, x="exit_reason", y="count")
        fig.update_layout(height=300)
        st.plotly_chart(fig, use_container_width=True)


# Signals
with tabs[5]:
    st.subheader(f"Signals ({len(signals_df)})")
    display_sigs = signals_df.copy()
    display_sigs["total_value"] = display_sigs["total_value"].round(0)
    st.dataframe(
        display_sigs.sort_values("signal_date", ascending=False),
        use_container_width=True,
        hide_index=True,
    )
    if not result.signals_skipped.empty:
        with st.expander(f"Skipped signals ({len(result.signals_skipped)})"):
            st.dataframe(
                result.signals_skipped.sort_values("signal_date", ascending=False),
                use_container_width=True,
                hide_index=True,
            )
