"""
================================================================================
 ORDER MANAGER — Order execution & position lifecycle
================================================================================
Implements Beck's full trade-management sequence from
    Reliance_Gartley_Entry_StopLoss_Exit_Beck_Method.pdf, Section 9:

    1. Gartley completes -> price touches Fibonacci retracement.   [scanner]
    2. Choose entry method.                                        [trade_rules]
    3. Enter.
    4. Place initial protective stop.
    5. Calculate initial risk.
    6. Target 1 = 50% of initial risk.
    7. Exit 1/3; move stop on remaining position.
    8. Target 2 = 100% of initial risk.
    9. Exit another 1/3; move final stop to entry.
    10. Manage final 1/3 with 3-bar trailing stop on next larger timeframe.

--------------------------------------------------------------------------------
IMPORTANT SCOPE NOTE — READ THIS
--------------------------------------------------------------------------------
This is a SIMULATED / PAPER-TRADING execution engine. It evaluates fills,
stops, and targets against historical/quote OHLC data pulled via
gartley_scanner.fetch_data() (yfinance). It does NOT place real orders with
any broker. No broker API credentials or order-routing integration were
provided, and none is assumed here.

To go live, this module's `place_entry_order` / `_exit_units` methods are
exactly where a real broker SDK call (e.g. order placement, order-status
polling, fill confirmation webhooks) would be plugged in — the state
machine and risk checks around them would not need to change.
================================================================================
"""

import uuid
from dataclasses import dataclass, field
from datetime import datetime
from typing import Dict, List, Optional

import pandas as pd

from config import AppConfig
from trade_rules import TradePlan, _is_inside_bar
from gartley_scanner import GartleyCluster, fetch_data
import risk_manager as rm

try:
    from gartley_scanner import _now_ist
except Exception:
    def _now_ist():
        return datetime.now()


# ==============================================================================
# STATUS LIFECYCLE (used by both the engine and the dashboard for color-coding)
# ==============================================================================
STATUS_PENDING_ENTRY = "PENDING_ENTRY"
STATUS_ENTRY_FILLED = "ENTRY_FILLED"
STATUS_TARGET1_HIT = "TARGET1_HIT"
STATUS_TARGET2_HIT = "TARGET2_HIT"
STATUS_STOPPED_OUT = "STOPPED_OUT"
STATUS_TRAILING_EXIT = "TRAILING_EXIT"
STATUS_MANUALLY_CLOSED = "MANUALLY_CLOSED"
STATUS_EXPIRED = "EXPIRED"
STATUS_REJECTED = "REJECTED"

CLOSED_STATUSES = {STATUS_STOPPED_OUT, STATUS_TRAILING_EXIT,
                    STATUS_MANUALLY_CLOSED, STATUS_EXPIRED, STATUS_REJECTED}
OPEN_STATUSES = {STATUS_PENDING_ENTRY, STATUS_ENTRY_FILLED,
                  STATUS_TARGET1_HIT, STATUS_TARGET2_HIT}

ORDER_TYPE_BY_ENTRY_METHOD = {
    "fibonacci": "LIMIT",          # exact-price limit order at the Fib level
    "one_bar_reversal": "STOP",    # breakout-style stop-entry order
    "harami": "STOP",
    "indicator": "MARKET_ON_SIGNAL",  # fills at the bar the indicator triggers
}


@dataclass
class Position:
    trade_id: str
    symbol: str
    direction: str            # long / short
    entry_method: str
    order_type: str

    planned_entry: float
    actual_entry: Optional[float] = None
    initial_stop: float = 0.0
    current_stop: float = 0.0
    target1: float = 0.0
    target2: float = 0.0

    qty_total: int = 0
    units: List[int] = field(default_factory=list)     # e.g. [33,33,34]
    units_filled_exit: List[bool] = field(default_factory=list)

    status: str = STATUS_PENDING_ENTRY
    bars_pending: int = 0     # how many refresh cycles it's waited unfilled

    opened_at: Optional[str] = None
    closed_at: Optional[str] = None
    exit_reason: str = ""

    realized_pnl: float = 0.0
    last_price: float = 0.0

    # Pattern context for display
    pattern_direction: str = ""     # bullish/bearish (Gartley terms)
    cluster_level: float = 0.0
    X_price: float = 0.0
    A_price: float = 0.0
    B_price: float = 0.0
    C_price: float = 0.0

    history: List[dict] = field(default_factory=list)

    def unrealized_pnl(self) -> float:
        remaining_qty = sum(q for q, filled in zip(self.units, self.units_filled_exit) if not filled)
        if remaining_qty == 0 or self.actual_entry is None:
            return 0.0
        diff = (self.last_price - self.actual_entry) if self.direction == "long" \
            else (self.actual_entry - self.last_price)
        return diff * remaining_qty

    def total_pnl(self) -> float:
        return self.realized_pnl + self.unrealized_pnl()

    def qty_remaining(self) -> int:
        return sum(q for q, filled in zip(self.units, self.units_filled_exit) if not filled)


class OrderManager:
    def __init__(self, config: AppConfig, logger):
        self.config = config
        self.logger = logger
        self.open_positions: Dict[str, Position] = {}
        self.closed_trades: List[Position] = []

    # --------------------------------------------------------------------
    # STEP 3/4/5: PLACE ENTRY ORDER  (creates a PENDING_ENTRY position)
    # --------------------------------------------------------------------
    def place_entry_order(self, cluster: GartleyCluster, plan: TradePlan) -> Optional[Position]:
        symbol = cluster.symbol

        # [SAFEGUARD] duplicate-order prevention
        if rm.has_active_position(self.open_positions, symbol):
            self.logger.warning(symbol, "ORDER",
                "Skipped: an active order/position already exists for this symbol "
                "(duplicate-order prevention).")
            return None

        # [SAFEGUARD] market-hours validation
        ok, msg = rm.is_market_open(self.config)
        if not ok:
            self.logger.warning(symbol, "ORDER", f"Skipped: {msg}")
            return None

        # [SAFEGUARD] portfolio-level cap
        ok, msg = rm.can_open_new_position(self.open_positions, self.config)
        if not ok:
            self.logger.warning(symbol, "RISK", msg)
            return None

        # [SAFEGUARD] position sizing based on max rupee risk (Beck: "share
        # quantity should be based on your own maximum rupee risk")
        sizing = rm.calculate_position_size(
            self.config.account_capital, self.config.risk_pct_per_trade,
            plan.entry_price, plan.initial_stop, self.config.scale_out_units,
        )
        if sizing.warning:
            self.logger.warning(symbol, "RISK", sizing.warning)
        if sizing.qty_total <= 0:
            self.logger.error(symbol, "RISK", "Trade rejected: position size is 0 "
                               "within your configured risk budget.")
            return None

        pos = Position(
            trade_id=str(uuid.uuid4())[:8],
            symbol=symbol,
            direction=plan.direction,
            entry_method=plan.entry_method,
            order_type=ORDER_TYPE_BY_ENTRY_METHOD.get(plan.entry_method, "LIMIT"),
            planned_entry=plan.entry_price,
            initial_stop=plan.initial_stop,
            current_stop=plan.initial_stop,
            target1=plan.target1,
            target2=plan.target2,
            qty_total=sizing.qty_total,
            units=sizing.units,
            units_filled_exit=[False] * len(sizing.units),
            status=STATUS_PENDING_ENTRY,
            pattern_direction=cluster.direction,
            cluster_level=cluster.cluster_level,
            X_price=cluster.X.price, A_price=cluster.A.price,
            B_price=cluster.B.price, C_price=cluster.C.price,
        )
        self._record(pos, "ORDER_PLACED",
                      f"{plan.entry_method} {plan.direction} order placed @ {plan.entry_price:.2f}, "
                      f"stop {plan.initial_stop:.2f}, qty {sizing.qty_total} ({sizing.units})")
        self.open_positions[symbol] = pos
        self.logger.success(symbol, "ORDER",
            f"Entry order PLACED ({plan.entry_method}, {plan.direction}) @ {plan.entry_price:.2f} "
            f"| qty {sizing.qty_total} | stop {plan.initial_stop:.2f}")
        return pos

    # --------------------------------------------------------------------
    # MONITORING: called every refresh cycle for every open position
    # --------------------------------------------------------------------
    def monitor_position(self, pos: Position, df: pd.DataFrame,
                          higher_tf_interval: Optional[str] = None):
        if df is None or df.empty:
            self.logger.error(pos.symbol, "POSITION",
                "No market data available this cycle — position NOT evaluated "
                "(stale). Will retry next refresh.")
            return

        bar = df.iloc[-1]
        pos.last_price = float(bar["Close"])

        if pos.status == STATUS_PENDING_ENTRY:
            self._check_entry_fill(pos, bar)
        elif pos.status in (STATUS_ENTRY_FILLED, STATUS_TARGET1_HIT, STATUS_TARGET2_HIT):
            self._check_exit_conditions(pos, df, higher_tf_interval)

        if pos.status in CLOSED_STATUSES and pos.symbol in self.open_positions:
            self.closed_trades.append(pos)
            del self.open_positions[pos.symbol]
            self.logger.record_closed_trade({
                "trade_id": pos.trade_id, "symbol": pos.symbol, "direction": pos.direction,
                "entry_method": pos.entry_method, "entry_price": pos.actual_entry,
                "stop_price": pos.current_stop, "target1": pos.target1, "target2": pos.target2,
                "qty_total": pos.qty_total, "opened_at": pos.opened_at, "closed_at": pos.closed_at,
                "exit_reason": pos.exit_reason, "realized_pnl": round(pos.realized_pnl, 2),
                "r_multiple": self._r_multiple(pos),
            })

    def _r_multiple(self, pos: Position) -> Optional[float]:
        if pos.actual_entry is None:
            return None
        risk_per_share = abs(pos.actual_entry - pos.initial_stop)
        if risk_per_share <= 0 or pos.qty_total <= 0:
            return None
        return round(pos.realized_pnl / (risk_per_share * pos.qty_total), 2)

    # --------------------------------------------------------------------
    # STEP 3: CHECK FILL (limit @ Fib level, or stop-entry breakout)
    # --------------------------------------------------------------------
    def _check_entry_fill(self, pos: Position, bar):
        filled = False
        fill_price = pos.planned_entry

        if pos.order_type == "LIMIT":
            # Fills if the bar's range contains the limit price.
            if bar["Low"] <= pos.planned_entry <= bar["High"]:
                filled = True
        elif pos.order_type == "STOP":
            if pos.direction == "long" and bar["High"] >= pos.planned_entry:
                filled = True
            elif pos.direction == "short" and bar["Low"] <= pos.planned_entry:
                filled = True
        else:  # MARKET_ON_SIGNAL (indicator method) -- trade_rules already
               # located the trigger bar/price when the plan was built.
            filled = True

        if filled:
            pos.actual_entry = fill_price
            pos.opened_at = _now_ist().strftime("%Y-%m-%d %H:%M:%S")
            pos.status = STATUS_ENTRY_FILLED
            self._record(pos, "ENTRY_FILLED", f"Filled @ {fill_price:.2f}")
            self.logger.success(pos.symbol, "ORDER", f"Entry FILLED @ {fill_price:.2f}")
            return

        pos.bars_pending += 1
        if pos.bars_pending >= self.config.order_expiry_bars:
            pos.status = STATUS_EXPIRED
            pos.closed_at = _now_ist().strftime("%Y-%m-%d %H:%M:%S")
            pos.exit_reason = "Order expired unfilled"
            self._record(pos, "EXPIRED",
                          f"Unfilled after {pos.bars_pending} bars — order expired "
                          f"(duplicate-order prevention: symbol now free for a new signal).")
            self.logger.warning(pos.symbol, "ORDER", "Entry order EXPIRED unfilled.")

    # --------------------------------------------------------------------
    # STEPS 6-10: STOP / TARGET1 / TARGET2 / TRAILING-STOP MONITORING
    # --------------------------------------------------------------------
    def _check_exit_conditions(self, pos: Position, df: pd.DataFrame,
                                higher_tf_interval: Optional[str]):
        bar = df.iloc[-1]
        is_long = pos.direction == "long"

        # ---- Full-position stop (before Target 1 is hit) ----
        if pos.status == STATUS_ENTRY_FILLED:
            stop_hit = (bar["Low"] <= pos.current_stop) if is_long else (bar["High"] >= pos.current_stop)
            if stop_hit:
                self._exit_all(pos, reason="Initial stop hit (before Target 1)")
                return
            target_hit = (bar["High"] >= pos.target1) if is_long else (bar["Low"] <= pos.target1)
            if target_hit:
                self._exit_unit(pos, unit_index=0, price=pos.target1, reason="Target 1 (50% risk) hit")
                # [BECK] "move the stop on the remaining two units upward by
                # 50% of the initial risk"
                risk = abs(pos.actual_entry - pos.initial_stop)
                move = risk * self.config.target1_risk_multiple
                pos.current_stop = pos.current_stop + move if is_long else pos.current_stop - move
                pos.status = STATUS_TARGET1_HIT
                self._record(pos, "STOP_MOVED",
                              f"Stop moved to {pos.current_stop:.2f} after Target 1 (per Beck's rule).")
                return

        # ---- After Target 1 (2 units remain) ----
        if pos.status == STATUS_TARGET1_HIT:
            stop_hit = (bar["Low"] <= pos.current_stop) if is_long else (bar["High"] >= pos.current_stop)
            if stop_hit:
                self._exit_all(pos, reason="Trailed stop hit after Target 1")
                return
            target_hit = (bar["High"] >= pos.target2) if is_long else (bar["Low"] <= pos.target2)
            if target_hit:
                self._exit_unit(pos, unit_index=1, price=pos.target2, reason="Target 2 (100% risk) hit")
                # [BECK] "move the protective stop on the final unit to the entry price"
                pos.current_stop = pos.actual_entry
                pos.status = STATUS_TARGET2_HIT
                self._record(pos, "STOP_MOVED",
                              f"Final unit stop moved to breakeven ({pos.actual_entry:.2f}) after Target 2.")
                return

        # ---- Final unit: 3-bar trailing stop on the NEXT LARGER TIMEFRAME ----
        if pos.status == STATUS_TARGET2_HIT:
            trail_stop = self._compute_trailing_stop(pos, higher_tf_interval)
            if trail_stop is not None:
                # [BECK] "the trailing stop should not be allowed below entry
                # on a long trade (or above entry on a short trade)"
                if is_long:
                    trail_stop = max(trail_stop, pos.actual_entry)
                    if trail_stop > pos.current_stop:
                        pos.current_stop = trail_stop
                else:
                    trail_stop = min(trail_stop, pos.actual_entry)
                    if trail_stop < pos.current_stop:
                        pos.current_stop = trail_stop

            stop_hit = (bar["Low"] <= pos.current_stop) if is_long else (bar["High"] >= pos.current_stop)
            if stop_hit:
                self._exit_unit(pos, unit_index=2, price=pos.current_stop,
                                 reason="3-bar trailing stop hit (final unit)", is_trailing=True)
                pos.status = STATUS_TRAILING_EXIT
                pos.closed_at = _now_ist().strftime("%Y-%m-%d %H:%M:%S")
                pos.exit_reason = "3-bar trailing stop hit"

    def _compute_trailing_stop(self, pos: Position, higher_tf_interval: Optional[str]) -> Optional[float]:
        """[BECK] Long: lowest low of the last 3 complete valid (non-inside)
        bars on the next larger timeframe. Short: highest high, mirrored."""
        if not higher_tf_interval:
            return None
        htf_df = fetch_data(pos.symbol, interval=higher_tf_interval, period="1y")
        if htf_df is None or len(htf_df) < 5:
            return None

        valid_bars = []
        for i in range(len(htf_df) - 1, 0, -1):
            if not _is_inside_bar(htf_df.iloc[i - 1], htf_df.iloc[i]):
                valid_bars.append(htf_df.iloc[i])
            if len(valid_bars) == self.config.trailing_stop_bars:
                break

        if len(valid_bars) < self.config.trailing_stop_bars:
            return None

        if pos.direction == "long":
            return min(b["Low"] for b in valid_bars)
        else:
            return max(b["High"] for b in valid_bars)

    # --------------------------------------------------------------------
    # EXIT HELPERS
    # --------------------------------------------------------------------
    def _exit_unit(self, pos: Position, unit_index: int, price: float, reason: str,
                    is_trailing: bool = False):
        if pos.units_filled_exit[unit_index]:
            return
        qty = pos.units[unit_index]
        exit_price = rm.apply_slippage(price, pos.direction, self.config) if is_trailing else price
        pnl = (exit_price - pos.actual_entry) * qty if pos.direction == "long" \
            else (pos.actual_entry - exit_price) * qty
        pos.realized_pnl += pnl
        pos.units_filled_exit[unit_index] = True
        self._record(pos, "UNIT_EXIT",
                      f"Exited unit {unit_index+1} ({qty} shares) @ {exit_price:.2f} "
                      f"[{reason}] | P&L this unit: {pnl:+.2f}")
        self.logger.success(pos.symbol, "POSITION",
            f"{reason}: exited {qty} shares @ {exit_price:.2f} (P&L {pnl:+.2f})")

    def _exit_all(self, pos: Position, reason: str, manual_price: Optional[float] = None):
        for i, filled in enumerate(pos.units_filled_exit):
            if not filled:
                price = manual_price if manual_price is not None else pos.current_stop
                is_stop_exit = manual_price is None
                exit_price = rm.apply_slippage(price, pos.direction, self.config) if is_stop_exit else price
                qty = pos.units[i]
                pnl = (exit_price - pos.actual_entry) * qty if pos.direction == "long" \
                    else (pos.actual_entry - exit_price) * qty
                pos.realized_pnl += pnl
                pos.units_filled_exit[i] = True
                self._record(pos, "UNIT_EXIT",
                              f"Exited unit {i+1} ({qty} shares) @ {exit_price:.2f} [{reason}]")
        pos.status = STATUS_MANUALLY_CLOSED if manual_price is not None else STATUS_STOPPED_OUT
        pos.closed_at = _now_ist().strftime("%Y-%m-%d %H:%M:%S")
        pos.exit_reason = reason
        self.logger.warning(pos.symbol, "POSITION", f"Position CLOSED: {reason} | "
                             f"Total P&L: {pos.realized_pnl:+.2f}")

    # --------------------------------------------------------------------
    # [SAFEGUARD] EMERGENCY / MANUAL EXIT — not in Beck's document, but
    # required for reliable operation (e.g. news event, system doubt).
    # --------------------------------------------------------------------
    def manual_exit(self, symbol: str, price: float):
        pos = self.open_positions.get(symbol)
        if pos is None:
            self.logger.error(symbol, "POSITION", "Manual exit failed: no open position found.")
            return
        self._exit_all(pos, reason="Manual exit (user-initiated)", manual_price=price)
        self.closed_trades.append(pos)
        del self.open_positions[symbol]
        self.logger.record_closed_trade({
            "trade_id": pos.trade_id, "symbol": pos.symbol, "direction": pos.direction,
            "entry_method": pos.entry_method, "entry_price": pos.actual_entry,
            "stop_price": pos.current_stop, "target1": pos.target1, "target2": pos.target2,
            "qty_total": pos.qty_total, "opened_at": pos.opened_at, "closed_at": pos.closed_at,
            "exit_reason": pos.exit_reason, "realized_pnl": round(pos.realized_pnl, 2),
            "r_multiple": self._r_multiple(pos),
        })

    def _record(self, pos: Position, event: str, detail: str):
        pos.history.append({
            "timestamp": _now_ist().strftime("%Y-%m-%d %H:%M:%S"),
            "event": event, "detail": detail,
        })

    # --------------------------------------------------------------------
    # [SAFEGUARD] POSITION PERSISTENCE / RECONCILIATION-ON-RESTART
    # Not covered by Beck's document. Without this, restarting the
    # dashboard process would silently forget any open position, which is
    # unacceptable for anything managing real capital. This persists
    # state to a local JSON file so a restart can reload exactly where it
    # left off.
    #
    # LIMITATION (explicitly flagged, not solved here): this reconciles
    # against this app's OWN last-known state, not against a live broker's
    # actual books. True broker reconciliation requires calling the
    # broker's position/order-status API on startup and diffing —
    # impossible without a broker integration, which is out of scope.
    # --------------------------------------------------------------------
    def save_state(self, path: str = "./logs/positions_state.json"):
        import json, os, dataclasses
        os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
        state = {
            "open_positions": {s: dataclasses.asdict(p) for s, p in self.open_positions.items()},
            "closed_trades": [dataclasses.asdict(p) for p in self.closed_trades[-200:]],
        }
        with open(path, "w") as f:
            json.dump(state, f, indent=2, default=str)

    def load_state(self, path: str = "./logs/positions_state.json"):
        import json, os
        if not os.path.exists(path):
            return
        try:
            with open(path) as f:
                state = json.load(f)
            for sym, d in state.get("open_positions", {}).items():
                self.open_positions[sym] = Position(**d)
            self.closed_trades = [Position(**d) for d in state.get("closed_trades", [])]
            self.logger.info("SYSTEM", "SYSTEM",
                f"Restored {len(self.open_positions)} open position(s) and "
                f"{len(self.closed_trades)} closed trade(s) from saved state.")
        except Exception as e:
            self.logger.error("SYSTEM", "SYSTEM", f"Failed to load saved state: {e}")
