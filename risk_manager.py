"""
================================================================================
 RISK MANAGER
================================================================================
Handles everything Beck's document explicitly leaves to the trader ("share
quantity should be based on your own maximum rupee risk") plus the
execution safeguards that are NOT covered by the document at all but are
required for reliable order execution. Every function below is labeled:

    [BECK]      - directly required/implied by the document
    [SAFEGUARD] - added technical safeguard, NOT a Beck trading rule

================================================================================
"""

from dataclasses import dataclass
from datetime import datetime
from typing import List, Optional, Tuple

from config import AppConfig

try:
    from gartley_scanner import IST, _now_ist
except Exception:
    IST = None
    def _now_ist():
        return datetime.now()


# ==============================================================================
# [SAFEGUARD] POSITION SIZING
# Beck's document (Section 4): "Beck's single-in/scale-out examples use
# three units/contracts. For an actual NSE equity trade, share quantity
# should be based on your own maximum rupee risk; three units is not a
# mandatory RELIANCE quantity."
# -> Beck tells us WHAT to base sizing on (max rupee risk) but gives no
#    formula. The formula below is a standard, clearly-flagged addition.
# ==============================================================================

@dataclass
class PositionSizeResult:
    qty_total: int
    units: List[int]          # quantity per scale-out unit (e.g. [33,33,34])
    risk_amount: float        # actual ₹ risked at this size
    warning: Optional[str] = None


def calculate_position_size(capital: float, risk_pct_per_trade: float,
                             entry_price: float, stop_price: float,
                             scale_out_units: int = 3) -> PositionSizeResult:
    """
    [SAFEGUARD] max_risk_amount = capital * risk_pct_per_trade / 100
                qty_total       = floor(max_risk_amount / risk_per_share)
    Then splits qty_total into `scale_out_units` roughly-equal integer
    units (Beck's 3-unit scale-out structure), remainder assigned to the
    final ("runner") unit.
    """
    risk_per_share = abs(entry_price - stop_price)
    if risk_per_share <= 0:
        return PositionSizeResult(0, [], 0.0,
            warning="Invalid risk per share (entry == stop). Cannot size position.")

    max_risk_amount = capital * (risk_pct_per_trade / 100.0)
    qty_total = int(max_risk_amount // risk_per_share)

    if qty_total <= 0:
        return PositionSizeResult(0, [], 0.0,
            warning=(f"Risk per share (Rs.{risk_per_share:.2f}) exceeds the max "
                     f"rupee risk budget (Rs.{max_risk_amount:.2f}) for this trade. "
                     f"0 shares can be bought within your risk limit — trade skipped."))

    if qty_total < scale_out_units:
        # Not enough quantity to split into Beck's 3-unit scale-out structure.
        # [SAFEGUARD] Flagged rather than silently forcing fractional units.
        return PositionSizeResult(
            qty_total, [qty_total], qty_total * risk_per_share,
            warning=(f"Position size ({qty_total} shares) is too small to split into "
                     f"{scale_out_units} scale-out units. Falling back to a SINGLE unit "
                     f"— Target1/Target2 scale-out logic will be skipped for this trade; "
                     f"only the final trailing-stop exit rule will apply.")
        )

    base = qty_total // scale_out_units
    remainder = qty_total % scale_out_units
    units = [base] * scale_out_units
    units[-1] += remainder   # remainder goes to the final/runner unit
    return PositionSizeResult(qty_total, units, qty_total * risk_per_share)


# ==============================================================================
# [SAFEGUARD] MARKET HOURS VALIDATION
# Not mentioned anywhere in Beck's document. New entry orders should not be
# placed outside NSE cash market hours. Already-open positions continue to
# be monitored regardless (a stop/target can still be simulated against
# after-hours or next-session data).
# ==============================================================================

def is_market_open(config: AppConfig, now: Optional[datetime] = None) -> Tuple[bool, str]:
    if not config.enforce_market_hours:
        return True, "Market-hours check disabled by user setting."

    now = now or _now_ist()
    if now.weekday() >= 5:   # Sat/Sun
        return False, "Market closed (weekend)."

    open_h, open_m = config.market_open_hm
    close_h, close_m = config.market_close_hm
    open_t = now.replace(hour=open_h, minute=open_m, second=0, microsecond=0)
    close_t = now.replace(hour=close_h, minute=close_m, second=0, microsecond=0)

    if now < open_t:
        return False, f"Market not yet open (opens {open_h:02d}:{open_m:02d} IST)."
    if now > close_t:
        return False, f"Market closed for the day (closed {close_h:02d}:{close_m:02d} IST)."
    return True, "Market open."

    # [SAFEGUARD-LIMITATION] This does not account for NSE trading holidays
    # (Diwali, Republic Day, etc.) since no holiday calendar was provided or
    # specified by Beck's document. A production system should plug in an
    # official NSE holiday calendar here.


# ==============================================================================
# [SAFEGUARD] DUPLICATE-ORDER PREVENTION
# Not mentioned in Beck's document. Prevents placing a second entry order
# for a symbol that already has an active (non-closed) position/order.
# ==============================================================================

ACTIVE_STATUSES = {"PENDING_ENTRY", "ENTRY_FILLED", "TARGET1_HIT", "TARGET2_HIT"}


def has_active_position(open_positions: dict, symbol: str) -> bool:
    pos = open_positions.get(symbol)
    return pos is not None and pos.status in ACTIVE_STATUSES


# ==============================================================================
# [SAFEGUARD] PORTFOLIO-LEVEL CAPS
# Not mentioned in Beck's document (which discusses single-trade mechanics
# only). Caps total number of concurrently open positions so the system
# doesn't over-commit capital across many simultaneous Gartley signals.
# ==============================================================================

def can_open_new_position(open_positions: dict, config: AppConfig) -> Tuple[bool, str]:
    active = [p for p in open_positions.values() if p.status in ACTIVE_STATUSES]
    if len(active) >= config.max_open_positions:
        return False, (f"Max concurrent positions reached "
                        f"({len(active)}/{config.max_open_positions}). "
                        f"New signal skipped until a position frees up.")
    return True, ""


# ==============================================================================
# [SAFEGUARD] SLIPPAGE MODEL
# Beck's document gives exact prices with zero slippage assumption. Applied
# ONLY to stop-loss / trailing-stop exits (true "market" style exits under
# adverse conditions) — never to the Fibonacci limit entry or to Target1/
# Target2 (which are treated as limit-style exits at the exact level).
# ==============================================================================

def apply_slippage(price: float, direction: str, config: AppConfig) -> float:
    """Worsens the fill price by config.slippage_pct_on_stops, in the
    direction that hurts the trade (lower fill for a long stop-exit,
    higher fill for a short stop-exit)."""
    slip = price * (config.slippage_pct_on_stops / 100.0)
    if direction == "long":
        return price - slip
    else:
        return price + slip
