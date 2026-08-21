# Gartley Trading Cockpit — README

A complete pipeline: **Scan → Signal → Order → Fill → Stop/Target Monitoring → Exit → History/P&L**,
built on Ross L. Beck's Gartley method (retracement + extension + cluster) and his
entry/stop-loss/target rules, wrapped in a Streamlit dashboard.

---

## 1. Files & separation of concerns

| File | Responsibility |
|---|---|
| `gartley_scanner.py` | **Scanning** — unchanged, existing functionality. Finds swing points, XA retracement, AB=CD extension, cluster, and setup-completion (price touching the Fib level). |
| `trade_rules.py` | **Signal generation** — turns a completed cluster into a `TradePlan` (entry price, stop, Target 1, Target 2) using Beck's four entry methods. Pure calculation, no order state. |
| `risk_manager.py` | **Risk management** — position sizing from your max-rupee-risk, market-hours validation, duplicate-order prevention, portfolio position caps, slippage model. |
| `order_manager.py` | **Order execution & position monitoring** — the state machine: `PENDING_ENTRY → ENTRY_FILLED → TARGET1_HIT → TARGET2_HIT → TRAILING_EXIT` (or `STOPPED_OUT` / `MANUALLY_CLOSED` / `EXPIRED` / `REJECTED`). Also handles the 3-bar trailing stop on the next larger timeframe, and JSON state persistence. |
| `trade_logger.py` | **Logging & audit trail** — every event (scan, signal, order, fill, exit) is timestamped, kept in-memory for the dashboard, and appended to CSV for a permanent audit trail. |
| `config.py` | All tunable settings in one place, each labeled `[BECK]` (from the document) or `[SAFEGUARD]` (added for reliability, not a trading rule). |
| `dashboard.py` | The Streamlit UI tying all of the above together. |

Run it with:
```bash
pip install streamlit yfinance pandas numpy scipy
streamlit run dashboard.py
python -m streamlit run dashboard.py
```

---

## 2. How Beck's rules map to the code

From `Reliance_Gartley_Entry_StopLoss_Exit_Beck_Method.pdf`, Section 9 ("Sequence to Memorize") —
this exact 10-step sequence is also the dashboard's signature UI element (the horizontal
"rail" on every signal/position card):

| # | Beck's rule | Where it lives |
|---|---|---|
| 1 | Gartley completes → price touches Fib retracement | `gartley_scanner.check_completion` (existing) |
| 2 | Choose entry method | `config.entry_method` (default `"fibonacci"` — Beck's own recommended starting point) |
| 3 | Enter | `order_manager._check_entry_fill` |
| 4 | Place initial protective stop, beyond X | `trade_rules.compute_initial_stop` |
| 5 | Calculate initial risk | `trade_rules.build_core_plan` |
| 6 | Target 1 = 50% of initial risk | `config.target1_risk_multiple = 0.50` |
| 7 | Exit 1/3; move stop on remaining position | `order_manager._check_exit_conditions` (Target-1 branch) |
| 8 | Target 2 = 100% of initial risk | `config.target2_risk_multiple = 1.00` |
| 9 | Exit another 1/3; move final stop to entry | `order_manager._check_exit_conditions` (Target-2 branch) |
| 10 | Manage final 1/3 with 3-bar trailing stop, next larger timeframe, never past entry | `order_manager._compute_trailing_stop` + `config.higher_timeframe_map` |

All four of Beck's entry methods (Fibonacci, 1-bar reversal, Harami, Stochastic %D indicator)
are implemented in `trade_rules.py`, selectable from the sidebar. Fibonacci is the default,
exactly as Beck recommends.

---

## 3. Missing requirements identified (not in Beck's document) and how each was handled

Beck's document is a trade-management guide, not a systems-reliability spec. The following
gaps are real and were addressed as clearly-labeled **`[SAFEGUARD]`** additions — never
presented as if they were Beck's trading rules:

| Gap | How it's addressed |
|---|---|
| **Position sizing formula** | Beck says size by "your own maximum rupee risk" but gives no formula. `risk_manager.calculate_position_size()` implements `qty = floor(max_risk_₹ / risk_per_share)`, split into Beck's 3 scale-out units. |
| **Duplicate-order prevention** | `risk_manager.has_active_position()` blocks a second order on a symbol that already has one active. |
| **Order-status verification** | Explicit state machine (`PENDING_ENTRY` → ... → closed states) rather than an implicit "did it work?" assumption. |
| **Failed-order / API/network-failure handling** | Every `fetch_data` call in the scan/monitor loop is wrapped in try/except; a failure is logged and skipped, not fatal to the whole scan. |
| **Partial fills** | **Not fully modeled** — flagged as a known limitation. Simulated fills assume a full fill when price trades through the level. A real broker can partial-fill; wiring a real broker API is where this would need to be revisited. |
| **Slippage** | Applied only to stop-loss / trailing-stop exits (true market-style exits), never to the Fibonacci limit entry or Target 1/2 (limit-style). Configurable in the sidebar. |
| **Market-hours validation** | `risk_manager.is_market_open()` blocks new entries outside NSE 09:15–15:30 IST. **Limitation:** no holiday calendar included — flagged in the dashboard's "Scope & known limitations" panel. |
| **Logging & audit history** | `trade_logger.py` — every event to CSV (`audit_log.csv`, `trade_history.csv`) plus a live Activity Log tab. |
| **Emergency/manual exit** | "🛑 Emergency Manual Exit" button on every open position card. |
| **Position reconciliation** | `order_manager.save_state()` / `load_state()` persist open positions to JSON so a restart doesn't silently lose track of them. **Limitation:** this reconciles against the app's own last save, not a live broker's books — true broker reconciliation needs a broker API integration, which is out of scope here. |
| **Portfolio-level exposure cap** | `config.max_open_positions` — new signals are skipped once the cap is hit, logged as a warning. |

---

## 4. Important scope note

**This is a paper-trading / simulation engine.** It scans real market structure and
simulates fills, stops, and targets against OHLC data — it does **not** place real
orders with any broker. No broker credentials or order-routing integration were
provided or assumed.

To go live, `order_manager.place_entry_order()` and the exit methods (`_exit_unit`,
`_exit_all`) are exactly where real broker SDK calls (order placement, fill
confirmation, order-status polling) would replace the simulated fill checks — the
state machine, risk checks, and dashboard would not need to change.

---

## 5. Dashboard tour (simplified — native Streamlit components only)

The dashboard was simplified to show **only what the code actually does** —
no decorative banners, custom card systems, or CSS theming. Every element on
screen maps directly to a real backend capability:

- **Header** — title, and 3 numbers that matter regardless of which tab
  you're on: Market (open/closed — gates new orders), Open Trades, Total
  Profit/Loss. Nothing else is duplicated at the top.
- **Sidebar** — Capital & Risk and Automation are always visible because
  they directly change position sizing and order placement. Everything
  else (timeframe, pattern sensitivity, entry method, etc.) is in a
  collapsed **"Advanced settings"** expander — real config values consumed
  by the scanner/trade rules, just not needed for everyday use.
- **Scan Stocks tab** — stock list (Excel upload / default list / manual
  entry) → preview → **Scan Stocks** button → results table. Table columns
  are exactly the fields the scanner and trade-rules engine produce: Stock,
  Signal, Current Price, Entry, Stop-Loss, Target, Risk/Reward, Pattern
  Level, Status. Only stocks that matched are shown.
- **Trade Signals tab** — one card per completed signal with the real
  entry/stop/target/risk-reward/quantity from `trade_rules` and
  `risk_manager`, and a **Place Order** button that calls
  `order_manager.place_entry_order()`.
- **Open Positions tab** — one card per live position with the real fields
  tracked by `order_manager.Position` (entry, current stop, both targets,
  shares remaining, P&L, status) and a **Close This Trade Now** button
  that calls `order_manager.manual_exit()`. Trade log is in a collapsed
  expander, not shown by default.
- **Trade History tab** — closed trades from `order_manager.closed_trades`,
  P&L summary metrics, and an equity curve — nothing invented beyond what
  the order manager already records.
- **Activity Log tab** — a plain table of `trade_logger` events, for
  troubleshooting only.

Removed from the previous version: the gradient hero banner, the separate
4-step guide row (redundant with the tab order), the custom badge/card/
stepper CSS system (replaced with native `st.badge`, `st.container(border=
True)`, and `st.metric`), the "About & limitations" panel (kept as one
caption line in the sidebar), and duplicate status displays that repeated
the same information in more than one place.

### Excel stock-list upload — how it works

1. User picks **"📁 Upload Excel File"** and uploads a `.xlsx`, `.xls`, or `.csv`.
2. The app looks for a column named something like `Symbol`, `Stock`,
   `Ticker`, or `Name` (case-insensitive) — if none is found, a clear error
   names the columns it did find and what's expected instead.
3. Symbols are cleaned: uppercased, whitespace stripped, `.NS` suffix
   removed, invalid entries (bad characters) filtered out and reported,
   duplicates removed and counted.
4. The cleaned list is previewed in an expandable table **before** scanning.
5. Clicking **Scan Stocks** scans every uploaded stock individually and
   shows only the ones that match — exactly the existing filtering
   behavior, unchanged.

This upload logic lives in `dashboard.py` (`load_stock_list_from_file`,
`find_symbol_column`, `clean_symbol_list`) as presentation-layer code — it
does not touch the scanning/trading logic in `gartley_scanner.py`,
`trade_rules.py`, `risk_manager.py`, or `order_manager.py`, all of which
are unchanged from the previous version.
