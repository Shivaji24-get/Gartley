"""
================================================================================
 CONFIG — Central, user-adjustable settings for the Gartley trading system
================================================================================
Every number a user might reasonably want to tune lives here in one place.

Each setting is labeled with its source:
  [BECK]     - value/rule comes directly from Ross L. Beck's document
  [SAFEGUARD]- not specified by Beck; added as a sensible technical
               safeguard for reliable execution (clearly flagged, never
               silently invented as if it were a "trading rule")
================================================================================
"""

from dataclasses import dataclass, field
from typing import List


@dataclass
class AppConfig:
    # ----------------------------------------------------------------
    # SCANNER SETTINGS (existing functionality, unchanged)
    # ----------------------------------------------------------------
    interval: str = "1d"
    period: str = "2y"
    swing_order: int = 5
    touch_tolerance_pct: float = 0.15
    cluster_tightness_pct: float = 3.0

    # ----------------------------------------------------------------
    # ENTRY METHOD [BECK — Section 2, four entry methods]
    # Beck: "Best starting point ... use the Fibonacci entry method
    # first. It is the simplest to calculate."
    # ----------------------------------------------------------------
    entry_method: str = "fibonacci"   # fibonacci | one_bar_reversal | harami | indicator

    # How many bars an unfilled entry order may remain PENDING before it
    # is auto-expired.  [SAFEGUARD] Beck's doc never says a Fib order
    # anticipating touch stays open forever — an expiry avoids stale
    # orders silently sitting active after the setup has gone cold.
    order_expiry_bars: int = 15

    # ----------------------------------------------------------------
    # STOP LOSS [BECK — Section 3]
    # "Beck places the initial protective stop just beyond the high/low
    # where the Gartley Pattern begins" (beyond X).
    # ----------------------------------------------------------------
    # Buffer beyond X, expressed as % of X's price. Beck's own example
    # (X=1200 -> stop=1199.0) is ~0.083%. Kept configurable since Beck
    # says "the exact buffer should respect the stock's tick size."
    # [BECK rule + SAFEGUARD default value]
    stop_buffer_pct: float = 0.10

    # ----------------------------------------------------------------
    # TARGETS [BECK — Sections 5 & 6]
    # Target 1 = 50% of initial risk. Target 2 = 100% of initial risk.
    # ----------------------------------------------------------------
    target1_risk_multiple: float = 0.50   # [BECK]
    target2_risk_multiple: float = 1.00   # [BECK]

    # ----------------------------------------------------------------
    # SCALE-OUT / POSITION STRUCTURE [BECK — single-in/scale-out method]
    # "Beck's single-in/scale-out examples use three units."
    # ----------------------------------------------------------------
    scale_out_units: int = 3   # [BECK example structure]

    # ----------------------------------------------------------------
    # FINAL-LEG TRAILING STOP [BECK — Section 7]
    # 3-bar trailing stop, on the NEXT LARGER TIMEFRAME, never below
    # entry (long) / above entry (short).
    # ----------------------------------------------------------------
    trailing_stop_bars: int = 3   # [BECK]
    higher_timeframe_map: dict = field(default_factory=lambda: {
        "5m": "15m", "15m": "1h", "30m": "1h", "60m": "1d", "1h": "1d",
        "1d": "1wk", "1wk": "1mo",
    })   # [BECK: "changes to the next larger timeframe" — mapping is
         # a reasonable, explicit interpretation of that instruction]

    # ----------------------------------------------------------------
    # POSITION SIZING & RISK MANAGEMENT
    # [SAFEGUARD] Beck's doc explicitly says: "share quantity should be
    # based on your own maximum rupee risk; three units is not a
    # mandatory RELIANCE quantity." Beck does NOT give a formula — this
    # is a standard, clearly-flagged risk-management addition.
    # ----------------------------------------------------------------
    account_capital: float = 500000.0        # ₹ total capital
    risk_pct_per_trade: float = 1.0          # max % of capital risked per trade
    max_open_positions: int = 5              # [SAFEGUARD] portfolio-level cap
    max_risk_pct_per_symbol_sector: float = 100.0  # placeholder for future sector caps

    # ----------------------------------------------------------------
    # SLIPPAGE [SAFEGUARD]
    # Beck's doc gives exact prices with no slippage model. Applied
    # only to STOP-LOSS / TRAILING-STOP exits (true market-style exits),
    # never to the Fibonacci limit entry or the limit-style targets.
    # ----------------------------------------------------------------
    slippage_pct_on_stops: float = 0.05

    # ----------------------------------------------------------------
    # MARKET HOURS VALIDATION [SAFEGUARD]
    # NSE cash session. New entry orders are only placed within these
    # hours; a position already open continues to be monitored.
    # ----------------------------------------------------------------
    market_open_hm: tuple = (9, 15)
    market_close_hm: tuple = (15, 30)
    enforce_market_hours: bool = True

    # ----------------------------------------------------------------
    # SYMBOL UNIVERSE
    # ----------------------------------------------------------------
    symbols: List[str] = field(default_factory=list)   # empty => scanner's NIFTY_50 default

    # ----------------------------------------------------------------
    # LOGGING / AUDIT [SAFEGUARD]
    # ----------------------------------------------------------------
    log_dir: str = "./logs"
    trade_history_csv: str = "./logs/trade_history.csv"
    audit_log_csv: str = "./logs/audit_log.csv"


DEFAULT_CONFIG = AppConfig()
