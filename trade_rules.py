"""
================================================================================
 TRADE RULES — Beck's Entry, Stop Loss & Target logic
================================================================================
Implements ONLY what is in:
    Reliance_Gartley_Entry_StopLoss_Exit_Beck_Method.pdf

This module turns a completed GartleyCluster (from gartley_scanner.py) into
a concrete TradePlan: entry price, initial stop, Target 1, Target 2, and the
scale-out structure. It does NOT place or manage orders — that is
order_manager.py's job. This file is pure "what should the numbers be."

--------------------------------------------------------------------------------
BECK'S SEQUENCE (from the document, Section 9 "Sequence to Memorize"):
    1. Gartley completes -> price touches Fibonacci retracement.
    2. Choose entry method.
    3. Enter.
    4. Place initial protective stop.
    5. Calculate initial risk.
    6. Target 1 = 50% of initial risk.
    7. Exit 1/3; move stop on remaining position.
    8. Target 2 = 100% of initial risk.
    9. Exit another 1/3; move final stop to entry.
    10. Manage final 1/3 with 3-bar trailing stop on next larger timeframe.
--------------------------------------------------------------------------------

FOUR ENTRY METHODS (Section 2 of the document):
    - fibonacci        : limit order at the exact Fib retracement level.
                          Beck: "Best starting point ... use the Fibonacci
                          entry method first. It is the simplest to calculate."
    - one_bar_reversal  : after Fib touch, buy 1 point above the previous
                          valid bar's high (ignore an inside bar).
    - harami            : after Fib touch, wait for Harami completion;
                          buy 1 point above the mother's high; stop 1 point
                          below its low.
    - indicator         : after Fib touch, use Stochastic %D crossing above
                          20% for a buy (Beck's own example indicator).

The DEFAULT is "fibonacci" — the other three are provided for completeness
since the document describes their rules explicitly, but Beck himself
recommends starting with the Fibonacci method.
================================================================================
"""

from dataclasses import dataclass
from typing import Optional
import pandas as pd
import numpy as np

from gartley_scanner import GartleyCluster


# ==============================================================================
# DATA STRUCTURES
# ==============================================================================

@dataclass
class TradePlan:
    symbol: str
    direction: str              # 'long' or 'short'
    entry_method: str
    entry_price: float
    entry_trigger_ready: bool   # True if the entry condition can be evaluated now
                                 # (Fibonacci = always True once setup is complete;
                                 #  the other 3 need bar-by-bar confirmation)
    initial_stop: float
    initial_risk_per_share: float
    target1: float               # 50% of initial risk
    target2: float               # 100% of initial risk
    scale_out_units: int
    risk_reward_target1: float
    risk_reward_target2: float
    notes: str = ""


# ==============================================================================
# STEP 1: BUILD THE CORE PLAN (entry, stop, targets)
#   -- valid for ALL entry methods; only the entry_price differs.
# ==============================================================================

def _direction_of(cluster: GartleyCluster) -> str:
    """Map scanner's 'bullish'/'bearish' to trading 'long'/'short'.
    bullish => XA moved up, D is a LOW => buy the dip => long.
    bearish => XA moved down, D is a HIGH => sell the rally => short."""
    return "long" if cluster.direction == "bullish" else "short"


def compute_initial_stop(cluster: GartleyCluster, stop_buffer_pct: float) -> float:
    """
    [BECK] "Beck places the initial protective stop just beyond the high/low
    where the Gartley Pattern begins." For a bullish Gartley: below X.
    For a bearish Gartley: above X.

    Buffer size: Beck's own worked example (X=1200 -> stop=1199.0) is a
    fixed illustrative buffer; the document says "the exact buffer should
    respect the stock's tick size and execution conditions" without giving
    a formula, so a configurable % buffer is used here (stop_buffer_pct).
    """
    buffer_amt = cluster.X.price * (stop_buffer_pct / 100.0)
    if _direction_of(cluster) == "long":
        return cluster.X.price - buffer_amt
    else:
        return cluster.X.price + buffer_amt


def build_core_plan(cluster: GartleyCluster, entry_price: float,
                     stop_buffer_pct: float,
                     target1_mult: float, target2_mult: float,
                     scale_out_units: int) -> TradePlan:
    direction = _direction_of(cluster)
    stop = compute_initial_stop(cluster, stop_buffer_pct)
    risk = abs(entry_price - stop)

    if risk <= 0:
        raise ValueError(f"{cluster.symbol}: computed non-positive risk "
                          f"(entry={entry_price}, stop={stop}). Skipping plan.")

    if direction == "long":
        target1 = entry_price + risk * target1_mult   # [BECK] 50% of risk
        target2 = entry_price + risk * target2_mult   # [BECK] 100% of risk
    else:
        target1 = entry_price - risk * target1_mult
        target2 = entry_price - risk * target2_mult

    return TradePlan(
        symbol=cluster.symbol,
        direction=direction,
        entry_method="",   # filled in by caller
        entry_price=entry_price,
        entry_trigger_ready=True,
        initial_stop=stop,
        initial_risk_per_share=risk,
        target1=target1,
        target2=target2,
        scale_out_units=scale_out_units,
        risk_reward_target1=target1_mult,
        risk_reward_target2=target2_mult,
    )


# ==============================================================================
# THE FOUR ENTRY METHODS
# ==============================================================================

def _is_inside_bar(prev_row, row) -> bool:
    """An inside bar's High/Low are both contained within the previous bar's
    High/Low. Beck: 'ignore an inside bar' when looking for the reversal
    reference bar."""
    return row["High"] <= prev_row["High"] and row["Low"] >= prev_row["Low"]


def entry_fibonacci(cluster: GartleyCluster, df: pd.DataFrame,
                     config_stop_buffer_pct: float, **_) -> TradePlan:
    """[BECK, default] Limit order at the exact Fibonacci retracement level."""
    plan = build_core_plan(cluster, entry_price=cluster.cluster_price,
                            stop_buffer_pct=config_stop_buffer_pct,
                            target1_mult=0.5, target2_mult=1.0,
                            scale_out_units=3)
    plan.entry_method = "fibonacci"
    plan.notes = "Limit entry at Fib retracement level (Beck's recommended default)."
    return plan


def entry_one_bar_reversal(cluster: GartleyCluster, df: pd.DataFrame,
                            config_stop_buffer_pct: float, **_) -> TradePlan:
    """
    [BECK] "After Fib is touched, buy 1 point above the previous valid
    bar's high for a long trade. Ignore an inside bar."
    (Mirrored for a short: sell 1 point below the previous valid bar's low.)

    Implementation: scan forward from the Fib-touch bar for the most recent
    non-inside bar, and set the trigger 1 tick beyond its high/low. Until
    price actually breaks that trigger, entry_trigger_ready=False (i.e. the
    order is a stop-entry order, not yet filled).
    """
    if cluster.touch_date is None or cluster.touch_date not in df.index:
        raise ValueError("Cannot evaluate 1-bar reversal entry: touch bar not found.")

    touch_pos = df.index.get_loc(cluster.touch_date)
    direction = _direction_of(cluster)
    one_point = max(cluster.cluster_price * 0.0005, 0.05)  # ~1 "point" proxy

    # Find the most recent valid (non-inside) bar at/after the touch
    valid_bar = None
    for i in range(touch_pos, min(touch_pos + 5, len(df))):
        if i == 0:
            continue
        if not _is_inside_bar(df.iloc[i - 1], df.iloc[i]):
            valid_bar = df.iloc[i]
            valid_bar_pos = i
            break

    if valid_bar is None:
        raise ValueError("No valid (non-inside) reversal bar found after Fib touch.")

    if direction == "long":
        trigger = valid_bar["High"] + one_point
    else:
        trigger = valid_bar["Low"] - one_point

    plan = build_core_plan(cluster, entry_price=trigger,
                            stop_buffer_pct=config_stop_buffer_pct,
                            target1_mult=0.5, target2_mult=1.0,
                            scale_out_units=3)
    plan.entry_method = "one_bar_reversal"

    # Has price already broken the trigger in bars AFTER the valid bar?
    filled = False
    for i in range(valid_bar_pos + 1, len(df)):
        if direction == "long" and df.iloc[i]["High"] >= trigger:
            filled = True
            break
        if direction == "short" and df.iloc[i]["Low"] <= trigger:
            filled = True
            break
    plan.entry_trigger_ready = filled
    plan.notes = ("1-bar reversal: buy/sell trigger 1 point beyond the last "
                   "valid (non-inside) bar since Fib touch.")
    return plan


def entry_harami(cluster: GartleyCluster, df: pd.DataFrame,
                  config_stop_buffer_pct: float, **_) -> TradePlan:
    """
    [BECK] "After Fib is touched, wait for Harami completion; buy 1 point
    above the mother's high; stop 1 point below its low."

    A bullish Harami = a large "mother" candle followed by a smaller candle
    fully contained within the mother's range (classic 2-candle pattern).
    """
    if cluster.touch_date is None or cluster.touch_date not in df.index:
        raise ValueError("Cannot evaluate Harami entry: touch bar not found.")

    touch_pos = df.index.get_loc(cluster.touch_date)
    direction = _direction_of(cluster)
    one_point = max(cluster.cluster_price * 0.0005, 0.05)

    mother = None
    for i in range(touch_pos, min(touch_pos + 10, len(df) - 1)):
        mo, ba = df.iloc[i], df.iloc[i + 1]
        contained = ba["High"] <= mo["High"] and ba["Low"] >= mo["Low"]
        mother_body = abs(mo["Close"] - mo["Open"])
        baby_body = abs(ba["Close"] - ba["Open"])
        if contained and baby_body < mother_body:
            mother = mo
            mother_pos = i
            break

    if mother is None:
        raise ValueError("No Harami (mother/baby) pattern found after Fib touch.")

    if direction == "long":
        trigger = mother["High"] + one_point
        harami_stop = mother["Low"] - one_point
    else:
        trigger = mother["Low"] - one_point
        harami_stop = mother["High"] + one_point

    plan = build_core_plan(cluster, entry_price=trigger,
                            stop_buffer_pct=config_stop_buffer_pct,
                            target1_mult=0.5, target2_mult=1.0,
                            scale_out_units=3)
    plan.entry_method = "harami"
    # Beck specifies the Harami stop explicitly (1 point beyond mother's
    # opposite extreme) -- this OVERRIDES the generic beyond-X stop for
    # this entry method only, exactly as the document describes.
    plan.initial_stop = harami_stop
    plan.initial_risk_per_share = abs(trigger - harami_stop)
    if plan.initial_risk_per_share <= 0:
        raise ValueError(f"{cluster.symbol}: Harami stop produced non-positive risk.")
    if direction == "long":
        plan.target1 = trigger + plan.initial_risk_per_share * 0.5
        plan.target2 = trigger + plan.initial_risk_per_share * 1.0
    else:
        plan.target1 = trigger - plan.initial_risk_per_share * 0.5
        plan.target2 = trigger - plan.initial_risk_per_share * 1.0

    filled = False
    for i in range(mother_pos + 2, len(df)):
        if direction == "long" and df.iloc[i]["High"] >= trigger:
            filled = True
            break
        if direction == "short" and df.iloc[i]["Low"] <= trigger:
            filled = True
            break
    plan.entry_trigger_ready = filled
    plan.notes = "Harami: entry 1pt beyond mother's high/low; stop 1pt beyond mother's opposite extreme."
    return plan


def _stochastic_pct_d(df: pd.DataFrame, k_period: int = 14, d_period: int = 3) -> pd.Series:
    low_min = df["Low"].rolling(k_period).min()
    high_max = df["High"].rolling(k_period).max()
    pct_k = 100 * (df["Close"] - low_min) / (high_max - low_min).replace(0, np.nan)
    pct_d = pct_k.rolling(d_period).mean()
    return pct_d


def entry_indicator(cluster: GartleyCluster, df: pd.DataFrame,
                     config_stop_buffer_pct: float, **_) -> TradePlan:
    """
    [BECK] "After Fib is touched, use the chosen indicator. Beck's example
    uses Stochastic %D crossing above 20% for a buy."
    (Mirrored for a short: %D crossing below 80%.)
    """
    if cluster.touch_date is None or cluster.touch_date not in df.index:
        raise ValueError("Cannot evaluate indicator entry: touch bar not found.")

    touch_pos = df.index.get_loc(cluster.touch_date)
    direction = _direction_of(cluster)
    pct_d = _stochastic_pct_d(df)

    entry_price = cluster.cluster_price
    filled = False
    for i in range(touch_pos + 1, len(df)):
        prev_d, curr_d = pct_d.iloc[i - 1], pct_d.iloc[i]
        if pd.isna(prev_d) or pd.isna(curr_d):
            continue
        if direction == "long" and prev_d <= 20 and curr_d > 20:
            entry_price = float(df.iloc[i]["Close"])
            filled = True
            break
        if direction == "short" and prev_d >= 80 and curr_d < 80:
            entry_price = float(df.iloc[i]["Close"])
            filled = True
            break

    plan = build_core_plan(cluster, entry_price=entry_price,
                            stop_buffer_pct=config_stop_buffer_pct,
                            target1_mult=0.5, target2_mult=1.0,
                            scale_out_units=3)
    plan.entry_method = "indicator"
    plan.entry_trigger_ready = filled
    plan.notes = "Stochastic %D crossing above 20% (long) / below 80% (short), Beck's example indicator."
    return plan


ENTRY_METHODS = {
    "fibonacci": entry_fibonacci,
    "one_bar_reversal": entry_one_bar_reversal,
    "harami": entry_harami,
    "indicator": entry_indicator,
}


def build_trade_plan(cluster: GartleyCluster, df: pd.DataFrame,
                      entry_method: str, stop_buffer_pct: float) -> TradePlan:
    """
    Main entry point for signal generation: given a completed GartleyCluster
    (setup_complete=True) and its OHLC dataframe, produce a TradePlan using
    the selected entry method.
    """
    if not cluster.setup_complete:
        raise ValueError(f"{cluster.symbol}: setup not complete (price hasn't "
                          f"touched the Fib retracement level yet) — no trade "
                          f"plan can be built until Beck's completion rule is met.")

    fn = ENTRY_METHODS.get(entry_method)
    if fn is None:
        raise ValueError(f"Unknown entry method '{entry_method}'. "
                          f"Choose from: {list(ENTRY_METHODS)}")
    return fn(cluster, df, config_stop_buffer_pct=stop_buffer_pct)
