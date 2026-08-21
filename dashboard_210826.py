# -*- coding: utf-8 -*-
"""
================================================================================
 TRADING DASHBOARD — Stock Pattern Scanner & Trade Assistant
================================================================================
Workflow: ADD STOCKS -> SCAN -> REVIEW SIGNALS -> PLACE ORDER ->
          TRACK OPEN TRADES -> HISTORY

Run with:
    streamlit run dashboard.py

SCOPE NOTE: This is a paper-trading (simulation) tool. It scans real market
data and simulates entries/exits — it does not place real orders with a
broker.

The scanning/trading LOGIC (gartley_scanner.py, trade_rules.py,
risk_manager.py, order_manager.py, trade_logger.py, config.py) is
unchanged — this file only presents it. No decorative UI elements were
added beyond what's needed to operate and understand that logic.
================================================================================
"""

import re
from datetime import datetime
from typing import Optional, List, Tuple

import pandas as pd
import streamlit as st

import gartley_scanner as gs
import trade_rules as tr
import risk_manager as rmgr
from config import AppConfig
from trade_logger import TradeLogger
from order_manager import (
    OrderManager,
    STATUS_PENDING_ENTRY, STATUS_ENTRY_FILLED, STATUS_TARGET1_HIT,
    STATUS_TARGET2_HIT, STATUS_STOPPED_OUT, STATUS_TRAILING_EXIT,
    STATUS_MANUALLY_CLOSED, STATUS_EXPIRED, STATUS_REJECTED,
)

st.set_page_config(page_title="Trading Dashboard", page_icon="📊", layout="wide")


# ==============================================================================
# EXCEL / CSV STOCK-LIST UPLOAD — parsing helpers
# ==============================================================================
CANDIDATE_SYMBOL_COLUMNS = [
    "symbol", "symbols", "stock", "stocks", "ticker", "tickers",
    "scrip", "scrip code", "stock symbol", "stock name", "name",
]


def find_symbol_column(df: pd.DataFrame) -> Optional[str]:
    lower_map = {str(c).strip().lower(): c for c in df.columns}
    for candidate in CANDIDATE_SYMBOL_COLUMNS:
        if candidate in lower_map:
            return lower_map[candidate]
    return None


def clean_symbol_list(raw_values: list) -> Tuple[List[str], List[str], int]:
    """Returns (clean_symbols, invalid_entries, duplicate_count)."""
    seen = set()
    cleaned, invalid = [], []
    dup_count = 0
    for v in raw_values:
        if pd.isna(v):
            continue
        s = str(v).strip().upper()
        if not s:
            continue
        s = re.sub(r"\.NS$", "", s)
        s = re.sub(r"\s+", "", s)
        if not re.match(r"^[A-Z0-9&\-]{1,20}$", s):
            invalid.append(str(v).strip())
            continue
        if s in seen:
            dup_count += 1
            continue
        seen.add(s)
        cleaned.append(s)
    return cleaned, invalid, dup_count


def load_stock_list_from_file(uploaded_file) -> Tuple[List[str], List[str], int, str]:
    """Reads an uploaded Excel/CSV file. Raises ValueError with a
    user-friendly message if the file can't be read or has no usable
    symbol column."""
    fname = (uploaded_file.name or "").lower()
    try:
        df = pd.read_csv(uploaded_file) if fname.endswith(".csv") else pd.read_excel(uploaded_file)
    except Exception as e:
        raise ValueError(f"Couldn't read this file. Make sure it's a valid .xlsx, .xls, "
                          f"or .csv file. ({e})")

    if df.empty or len(df.columns) == 0:
        raise ValueError("The uploaded file appears to be empty.")

    col = find_symbol_column(df)
    if col is None:
        raise ValueError(
            "No stock symbol column found. Please include a column named "
            "'Symbol', 'Stock', or 'Ticker'. "
            f"Columns found: {', '.join(str(c) for c in df.columns)}"
        )

    cleaned, invalid, dup_count = clean_symbol_list(df[col].tolist())
    if not cleaned:
        raise ValueError(f"No valid stock symbols were found in the '{col}' column.")
    return cleaned, invalid, dup_count, col


# ==============================================================================
# SESSION STATE
# ==============================================================================
if "config" not in st.session_state:
    st.session_state.config = AppConfig()
if "logger" not in st.session_state:
    st.session_state.logger = TradeLogger(
        audit_log_csv=st.session_state.config.audit_log_csv,
        trade_history_csv=st.session_state.config.trade_history_csv,
    )
if "order_manager" not in st.session_state:
    st.session_state.order_manager = OrderManager(st.session_state.config, st.session_state.logger)
    st.session_state.order_manager.load_state()
if "scan_results" not in st.session_state:
    st.session_state.scan_results = []
if "scan_errors" not in st.session_state:
    st.session_state.scan_errors = []
if "last_scan_time" not in st.session_state:
    st.session_state.last_scan_time = None
if "auto_refresh" not in st.session_state:
    st.session_state.auto_refresh = False
if "auto_execute" not in st.session_state:
    st.session_state.auto_execute = False
if "df_cache" not in st.session_state:
    st.session_state.df_cache = {}
if "stock_list" not in st.session_state:
    st.session_state.stock_list = []
if "has_scanned" not in st.session_state:
    st.session_state.has_scanned = False

cfg: AppConfig = st.session_state.config
om: OrderManager = st.session_state.order_manager
logger: TradeLogger = st.session_state.logger


# ==============================================================================
# SIDEBAR — only real, functional settings
# ==============================================================================
with st.sidebar:
    st.header("Settings")

    st.subheader("Capital & Risk")
    cfg.account_capital = st.number_input("Trading capital (₹)", min_value=10000.0,
                                           value=float(cfg.account_capital), step=10000.0)
    cfg.risk_pct_per_trade = st.slider("Max risk per trade (%)", 0.1, 5.0,
                                        cfg.risk_pct_per_trade, 0.1,
                                        help="Maximum you're willing to lose on one trade, "
                                             "as a % of your capital.")

    st.subheader("Automation")
    st.session_state.auto_execute = st.toggle(
        "Auto-place orders on new signals", value=st.session_state.auto_execute,
        help="Off = you place orders manually from the Trade Signals tab. "
             "On = orders are placed automatically when a signal is ready.")
    st.session_state.auto_refresh = st.toggle("Auto-refresh", value=st.session_state.auto_refresh)
    refresh_secs = st.number_input("Every (seconds)", min_value=15, max_value=600, value=60, step=15,
                                    disabled=not st.session_state.auto_refresh)

    with st.expander("Advanced settings"):
        cfg.interval = st.selectbox("Chart timeframe", ["1d", "1h", "60m", "30m", "15m", "1wk"],
                                     index=["1d","1h","60m","30m","15m","1wk"].index(cfg.interval)
                                     if cfg.interval in ["1d","1h","60m","30m","15m","1wk"] else 0)
        cfg.period = st.selectbox("History length", ["6mo", "1y", "2y", "5y", "30d", "60d"],
                                   index=["6mo","1y","2y","5y","30d","60d"].index(cfg.period)
                                   if cfg.period in ["6mo","1y","2y","5y","30d","60d"] else 2)
        cfg.swing_order = st.slider("Pattern sensitivity", 2, 15, cfg.swing_order)
        cfg.cluster_tightness_pct = st.slider("Match strictness (%)", 0.5, 10.0, cfg.cluster_tightness_pct, 0.5)
        cfg.touch_tolerance_pct = st.slider("Entry-touch tolerance (%)", 0.05, 1.0, cfg.touch_tolerance_pct, 0.05)
        cfg.entry_method = st.selectbox("Entry method", ["fibonacci", "one_bar_reversal", "harami", "indicator"],
                                         index=["fibonacci","one_bar_reversal","harami","indicator"].index(cfg.entry_method))
        cfg.stop_buffer_pct = st.slider("Stop-loss buffer (%)", 0.02, 1.0, cfg.stop_buffer_pct, 0.02)
        cfg.max_open_positions = st.slider("Max open trades", 1, 15, cfg.max_open_positions)
        cfg.slippage_pct_on_stops = st.slider("Slippage on exits (%)", 0.0, 0.5, cfg.slippage_pct_on_stops, 0.01)
        cfg.enforce_market_hours = st.checkbox("Only trade during market hours", value=cfg.enforce_market_hours)

    if st.button("Save progress", width="stretch"):
        om.save_state()
        st.success("Saved.")

    st.caption("Simulation only — no real orders are sent to a broker.")


# ==============================================================================
# CORE ENGINE CYCLE (unchanged logic)
# ==============================================================================

def higher_tf_for(interval: str) -> str:
    return cfg.higher_timeframe_map.get(interval, "1wk")


def run_full_cycle(symbols: List[str], progress_area=None):
    st.session_state.scan_results = []
    st.session_state.scan_errors = []
    st.session_state.df_cache = {}

    progress = (progress_area or st).progress(0.0, text=f"Scanning {len(symbols)} stocks…")
    for i, sym in enumerate(symbols):
        try:
            df = gs.fetch_data(sym, interval=cfg.interval, period=cfg.period)
            if df is None or df.empty:
                logger.error(sym, "SCAN", "No data returned.")
                st.session_state.scan_errors.append(sym)
                continue
            st.session_state.df_cache[sym] = df
            clusters = gs.scan_symbol(sym, cfg.interval, cfg.period, cfg.swing_order,
                                       cfg.touch_tolerance_pct)
            st.session_state.scan_results.extend(clusters)
        except Exception as e:
            logger.error(sym, "SCAN", f"Scan failed: {e}")
            st.session_state.scan_errors.append(sym)
        progress.progress((i + 1) / max(len(symbols), 1), text=f"Scanning… {sym} ({i+1}/{len(symbols)})")
    progress.empty()

    logger.info("SYSTEM", "SCAN",
        f"Scan complete: {len(symbols)} stocks checked, {len(st.session_state.scan_results)} "
        f"matched, {len(st.session_state.scan_errors)} error(s).")

    for cluster in st.session_state.scan_results:
        if not cluster.setup_complete:
            continue
        df = st.session_state.df_cache.get(cluster.symbol)
        if df is None or rmgr.has_active_position(om.open_positions, cluster.symbol):
            continue
        try:
            plan = tr.build_trade_plan(cluster, df, cfg.entry_method, cfg.stop_buffer_pct)
        except Exception as e:
            logger.warning(cluster.symbol, "SIGNAL", f"Entry not ready yet: {e}")
            continue
        logger.success(cluster.symbol, "SIGNAL",
            f"Signal generated ({plan.entry_method}, {plan.direction}) — "
            f"entry {plan.entry_price:.2f}, stop {plan.initial_stop:.2f}.")
        if st.session_state.auto_execute and plan.entry_trigger_ready:
            om.place_entry_order(cluster, plan)

    for sym, pos in list(om.open_positions.items()):
        try:
            df = gs.fetch_data(sym, interval=cfg.interval, period=cfg.period)
            om.monitor_position(pos, df, higher_tf_interval=higher_tf_for(cfg.interval))
        except Exception as e:
            logger.error(sym, "POSITION", f"Monitoring failed this cycle: {e}")

    om.save_state()
    st.session_state.last_scan_time = gs._now_ist() if hasattr(gs, "_now_ist") else datetime.now()
    st.session_state.has_scanned = True


if st.session_state.auto_refresh:
    st.markdown(f"<meta http-equiv='refresh' content='{int(refresh_secs)}'>", unsafe_allow_html=True)


# ==============================================================================
# STATUS LABELS (plain English)
# ==============================================================================

def friendly_status(status: Optional[str], entry_ready: Optional[bool] = None) -> Tuple[str, str]:
    """Returns (label, st.badge color)."""
    if status is None:
        return (("Ready to Enter", "green") if entry_ready else ("Waiting for Entry", "gray"))
    return {
        STATUS_PENDING_ENTRY: ("Order Pending", "orange"),
        STATUS_ENTRY_FILLED: ("Position Open", "blue"),
        STATUS_TARGET1_HIT: ("Target 1 Hit", "blue"),
        STATUS_TARGET2_HIT: ("Target 2 Hit", "blue"),
        STATUS_STOPPED_OUT: ("Stopped Out", "red"),
        STATUS_TRAILING_EXIT: ("Closed — Profit", "green"),
        STATUS_MANUALLY_CLOSED: ("Manually Closed", "gray"),
        STATUS_EXPIRED: ("Expired", "gray"),
        STATUS_REJECTED: ("Rejected", "red"),
    }.get(status, (status, "gray"))


# ==============================================================================
# HEADER
# ==============================================================================
st.title("📊 Trading Dashboard")
st.caption("Scan stocks for a chart pattern, then track entries, stop-loss and targets.")

market_open, market_msg = rmgr.is_market_open(cfg)
open_positions_count = len(om.open_positions)
total_realized = sum(p.realized_pnl for p in om.closed_trades) + \
                 sum(p.realized_pnl for p in om.open_positions.values())

m1, m2, m3 = st.columns(3)
m1.metric("Market", "Open" if market_open else "Closed")
m2.metric("Open Trades", open_positions_count)
m3.metric("Total Profit/Loss", f"₹{total_realized:,.0f}")

if not market_open:
    st.caption(f"{market_msg} Open trades are still monitored; new orders wait until the market opens.")
if st.session_state.scan_errors:
    st.warning(f"Couldn't fetch data for: {', '.join(st.session_state.scan_errors)}. "
               f"They'll be retried on the next scan.")

st.divider()


# ==============================================================================
# TABS
# ==============================================================================
tab_scan, tab_signals, tab_positions, tab_history, tab_log = st.tabs(
    ["Scan Stocks", "Trade Signals", "Open Positions", "Trade History", "Activity Log"]
)

# ------------------------------------------------------------------------
# TAB 1 — STOCK LIST + SCAN + RESULTS
# ------------------------------------------------------------------------
with tab_scan:
    st.subheader("1. Stock list")
    source_choice = st.radio(
        "Where should your stock list come from?",
        ["Upload Excel file", "Default list (Nifty 50)", "Type symbols manually"],
        horizontal=True,
    )

    if source_choice == "Upload Excel file":
        st.caption("File needs one column with stock symbols (e.g. a column named 'Symbol').")
        uploaded = st.file_uploader("Upload file", type=["xlsx", "xls", "csv"], label_visibility="collapsed")
        if uploaded is not None:
            try:
                clean, invalid, dup_count, col_used = load_stock_list_from_file(uploaded)
                st.session_state.stock_list = clean
                st.success(f"Excel file uploaded successfully — {len(clean)} stock(s) loaded.")
                if dup_count:
                    st.caption(f"Removed {dup_count} duplicate symbol(s).")
                if invalid:
                    st.caption(f"Skipped {len(invalid)} invalid entr{'y' if len(invalid)==1 else 'ies'}: "
                               f"{', '.join(invalid[:10])}{'…' if len(invalid) > 10 else ''}")
            except ValueError as e:
                st.error(str(e))
                st.session_state.stock_list = []

    elif source_choice == "Default list (Nifty 50)":
        st.session_state.stock_list = list(gs.NIFTY_50)
        st.info(f"{len(st.session_state.stock_list)} stocks loaded.")

    else:
        manual_text = st.text_area("Stock symbols (comma-separated)", value="RELIANCE, TCS, INFY", height=80)
        clean, invalid, dup_count = clean_symbol_list(manual_text.split(","))
        st.session_state.stock_list = clean
        if clean:
            st.success(f"{len(clean)} stock(s) loaded.")
        if invalid:
            st.caption(f"Ignored invalid entries: {', '.join(invalid)}")

    if st.session_state.stock_list:
        with st.expander(f"View stock list ({len(st.session_state.stock_list)})"):
            st.dataframe(pd.DataFrame({"Stock Symbol": st.session_state.stock_list}),
                         hide_index=True, width="stretch")
    else:
        st.warning("No stocks loaded yet.")

    st.divider()
    st.subheader("2. Scan")
    scan_disabled = len(st.session_state.stock_list) == 0
    scan_clicked = st.button("Scan Stocks", type="primary", disabled=scan_disabled)
    progress_area = st.empty()

    if scan_clicked:
        st.info(f"Scanning {len(st.session_state.stock_list)} stocks…")
        run_full_cycle(st.session_state.stock_list, progress_area=progress_area)
        n_matched = len(st.session_state.scan_results)
        if n_matched > 0:
            st.success(f"{n_matched} stock{'s' if n_matched != 1 else ''} matched the conditions "
                       f"out of {len(st.session_state.stock_list)} scanned.")
        else:
            st.info(f"No stocks matched the conditions. ({len(st.session_state.stock_list)} scanned)")

    st.divider()
    st.subheader("3. Results")
    completed = [c for c in st.session_state.scan_results if c.setup_complete]

    if not st.session_state.has_scanned:
        st.caption("Results will appear here after you scan.")
    elif not st.session_state.scan_results:
        st.caption("No stocks matched this time.")
    else:
        rows = []
        for c in st.session_state.scan_results:
            df = st.session_state.df_cache.get(c.symbol)
            entry = stop = target = rr = "—"
            if c.setup_complete and df is not None:
                try:
                    plan = tr.build_trade_plan(c, df, cfg.entry_method, cfg.stop_buffer_pct)
                    entry = f"₹{plan.entry_price:,.2f}"
                    stop = f"₹{plan.initial_stop:,.2f}"
                    target = f"₹{plan.target2:,.2f}"
                    rr = f"1 : {plan.risk_reward_target2:.1f}"
                except Exception:
                    pass
            rows.append({
                "Stock": c.symbol,
                "Signal": "BUY" if c.direction == "bullish" else "SELL",
                "Current Price": f"₹{c.current_price:,.2f}",
                "Entry": entry,
                "Stop-Loss": stop,
                "Target": target,
                "Risk/Reward": rr,
                "Pattern Level": f"{c.cluster_level*100:.1f}%",
                "Status": "Ready" if c.setup_complete else "Forming",
            })
        st.dataframe(pd.DataFrame(rows), hide_index=True, width="stretch")
        st.caption(f"Scanned {len(st.session_state.stock_list)} · matched {len(rows)} · "
                   f"ready to trade {len(completed)}.")

# ------------------------------------------------------------------------
# TAB 2 — TRADE SIGNALS
# ------------------------------------------------------------------------
with tab_signals:
    st.subheader("Signals ready to trade")
    st.caption("A signal appears once a stock's entry condition is met. Placing an order is a "
               "separate step — nothing happens automatically unless Auto-place is on.")

    complete_clusters = [c for c in st.session_state.scan_results if c.setup_complete]
    if not complete_clusters:
        st.info("No trade signals right now. Run a scan from the Scan Stocks tab first.")

    for c in complete_clusters:
        df = st.session_state.df_cache.get(c.symbol)
        existing_pos = om.open_positions.get(c.symbol)

        with st.container(border=True):
            col_title, col_price = st.columns([3, 1])
            with col_title:
                st.markdown(f"**{c.symbol}**")
                st.badge("BUY" if c.direction == "bullish" else "SELL",
                         color="green" if c.direction == "bullish" else "red")
            with col_price:
                st.metric("Current Price", f"₹{c.current_price:,.2f}")

            if existing_pos:
                label, color = friendly_status(existing_pos.status)
                st.badge(label, color=color)
            elif df is not None:
                try:
                    plan = tr.build_trade_plan(c, df, cfg.entry_method, cfg.stop_buffer_pct)
                    sizing = rmgr.calculate_position_size(cfg.account_capital, cfg.risk_pct_per_trade,
                                                           plan.entry_price, plan.initial_stop,
                                                           cfg.scale_out_units)
                    k1, k2, k3, k4 = st.columns(4)
                    k1.metric("Entry", f"₹{plan.entry_price:,.2f}")
                    k2.metric("Stop-Loss", f"₹{plan.initial_stop:,.2f}")
                    k3.metric("Target", f"₹{plan.target2:,.2f}")
                    k4.metric("Risk : Reward", f"1 : {plan.risk_reward_target2:.1f}")
                    st.caption(f"Quantity: {sizing.qty_total} shares · Amount at risk: ₹{sizing.risk_amount:,.0f}")

                    if sizing.warning:
                        st.warning(sizing.warning)

                    if st.button("Place Order", key=f"place_{c.symbol}",
                                 disabled=not plan.entry_trigger_ready, type="primary"):
                        new_pos = om.place_entry_order(c, plan)
                        if new_pos:
                            st.success(f"Order placed for {c.symbol}.")
                            st.rerun()
                        else:
                            st.error("Order was not placed — see Activity Log for the reason.")
                    if not plan.entry_trigger_ready:
                        st.caption("Waiting for the entry trigger to confirm.")
                except ValueError:
                    st.caption("Signal found — entry not confirmed yet.")
            else:
                st.error("Price data unavailable right now.")

# ------------------------------------------------------------------------
# TAB 3 — OPEN POSITIONS
# ------------------------------------------------------------------------
with tab_positions:
    st.subheader("Open trades")
    st.caption("Stop-loss and targets are checked automatically every time you scan.")

    if not om.open_positions:
        st.info("No open trades right now.")

    for sym, pos in om.open_positions.items():
        with st.container(border=True):
            col_title, col_pnl = st.columns([3, 1])
            with col_title:
                st.markdown(f"**{pos.symbol}**")
                st.badge("BUY" if pos.direction == "long" else "SELL",
                         color="green" if pos.direction == "long" else "red")
                label, color = friendly_status(pos.status)
                st.badge(label, color=color)
            with col_pnl:
                upnl = pos.unrealized_pnl()
                total = pos.realized_pnl + upnl
                st.metric("Profit/Loss", f"₹{total:,.0f}")

            k1, k2, k3, k4, k5 = st.columns(5)
            k1.metric("Entry", f"₹{(pos.actual_entry or 0):,.2f}")
            k2.metric("Stop-Loss", f"₹{pos.current_stop:,.2f}")
            k3.metric("Target 1", f"₹{pos.target1:,.2f}")
            k4.metric("Target 2", f"₹{pos.target2:,.2f}")
            k5.metric("Shares Left", f"{pos.qty_remaining()}/{pos.qty_total}")

            with st.expander("Trade log"):
                if pos.history:
                    st.dataframe(pd.DataFrame(pos.history), hide_index=True, width="stretch")
                else:
                    st.caption("No events yet.")

            if st.button("Close This Trade Now", key=f"exit_{sym}"):
                om.manual_exit(sym, pos.last_price)
                om.save_state()
                st.success(f"{sym} closed at ₹{pos.last_price:.2f}.")
                st.rerun()

# ------------------------------------------------------------------------
# TAB 4 — TRADE HISTORY
# ------------------------------------------------------------------------
with tab_history:
    st.subheader("Trade history")

    all_trades = om.closed_trades
    if not all_trades:
        st.info("No closed trades yet.")
    else:
        wins = [p for p in all_trades if p.realized_pnl > 0]
        total_pnl = sum(p.realized_pnl for p in all_trades)
        win_rate = len(wins) / len(all_trades) * 100
        avg_r = pd.Series([om._r_multiple(p) for p in all_trades if om._r_multiple(p) is not None]).mean()

        c1, c2, c3, c4 = st.columns(4)
        c1.metric("Total Profit/Loss", f"₹{total_pnl:,.0f}")
        c2.metric("Win Rate", f"{win_rate:.0f}%")
        c3.metric("Trades Closed", f"{len(all_trades)}")
        c4.metric("Avg. Return", f"{avg_r:.2f}R" if pd.notna(avg_r) else "—")

        equity = pd.DataFrame({
            "Trade #": range(1, len(all_trades) + 1),
            "Cumulative Profit/Loss (₹)": pd.Series([p.realized_pnl for p in all_trades]).cumsum(),
        }).set_index("Trade #")
        st.line_chart(equity)

        rows = []
        for p in all_trades:
            rows.append({
                "Stock": p.symbol, "Signal": "BUY" if p.direction == "long" else "SELL",
                "Entry": round(p.actual_entry or 0, 2),
                "Profit/Loss": round(p.realized_pnl, 2),
                "Exit Reason": p.exit_reason,
                "Opened": p.opened_at, "Closed": p.closed_at,
            })
        hist_df = pd.DataFrame(rows)
        st.dataframe(hist_df, hide_index=True, width="stretch")
        st.download_button("Download CSV", data=hist_df.to_csv(index=False), file_name="trade_history.csv")

# ------------------------------------------------------------------------
# TAB 5 — ACTIVITY LOG
# ------------------------------------------------------------------------
with tab_log:
    st.subheader("Activity log")
    st.caption("A record of scans, signals, orders, and trade events.")

    level_filter = st.multiselect("Filter", ["INFO", "SUCCESS", "WARNING", "ERROR"],
                                   default=["SUCCESS", "WARNING", "ERROR"])
    events = [e for e in logger.recent(300) if e.level in level_filter]
    if not events:
        st.caption("No matching log entries yet.")
    else:
        st.dataframe(pd.DataFrame([{
            "Time": e.timestamp, "Level": e.level, "Stage": e.stage,
            "Symbol": e.symbol, "Message": e.message,
        } for e in events]), hide_index=True, width="stretch")
