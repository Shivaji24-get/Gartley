"""
================================================================================
 TRADE LOGGER — Audit trail & activity feed
================================================================================
[SAFEGUARD] Beck's document says nothing about logging — this entire module
is a missing-requirement addition. Without it, there is no audit trail of
why an order was placed, filled, rejected, or why a position was exited,
which is essential for reviewing/debugging a semi-automated trading system.

Two outputs:
  1. In-memory list (self.events) -> powers the dashboard's live Activity Log
  2. CSV file (config.audit_log_csv) -> persists across sessions
================================================================================
"""

import csv
import os
from dataclasses import dataclass, asdict
from datetime import datetime
from typing import List, Optional

try:
    from gartley_scanner import _now_ist
except Exception:
    def _now_ist():
        return datetime.now()


@dataclass
class LogEvent:
    timestamp: str
    level: str        # INFO | SUCCESS | WARNING | ERROR
    symbol: str
    stage: str         # SCAN | SIGNAL | ORDER | POSITION | RISK | SYSTEM
    message: str


class TradeLogger:
    def __init__(self, audit_log_csv: str = "./logs/audit_log.csv",
                 trade_history_csv: str = "./logs/trade_history.csv"):
        self.audit_log_csv = audit_log_csv
        self.trade_history_csv = trade_history_csv
        self.events: List[LogEvent] = []
        os.makedirs(os.path.dirname(audit_log_csv) or ".", exist_ok=True)
        os.makedirs(os.path.dirname(trade_history_csv) or ".", exist_ok=True)
        self._ensure_csv_headers()

    def _ensure_csv_headers(self):
        if not os.path.exists(self.audit_log_csv):
            with open(self.audit_log_csv, "w", newline="") as f:
                csv.writer(f).writerow(["timestamp", "level", "symbol", "stage", "message"])
        if not os.path.exists(self.trade_history_csv):
            with open(self.trade_history_csv, "w", newline="") as f:
                csv.writer(f).writerow([
                    "trade_id", "symbol", "direction", "entry_method",
                    "entry_price", "stop_price", "target1", "target2",
                    "qty_total", "opened_at", "closed_at", "exit_reason",
                    "realized_pnl", "r_multiple",
                ])

    def log(self, level: str, symbol: str, stage: str, message: str):
        evt = LogEvent(
            timestamp=_now_ist().strftime("%Y-%m-%d %H:%M:%S"),
            level=level, symbol=symbol, stage=stage, message=message,
        )
        self.events.append(evt)
        with open(self.audit_log_csv, "a", newline="") as f:
            csv.writer(f).writerow(list(asdict(evt).values()))
        return evt

    def info(self, symbol, stage, message):
        return self.log("INFO", symbol, stage, message)

    def success(self, symbol, stage, message):
        return self.log("SUCCESS", symbol, stage, message)

    def warning(self, symbol, stage, message):
        return self.log("WARNING", symbol, stage, message)

    def error(self, symbol, stage, message):
        return self.log("ERROR", symbol, stage, message)

    def record_closed_trade(self, row: dict):
        """Appends one finished trade (fully or partially closed unit) to
        the persistent trade-history CSV for audit / P&L reporting."""
        with open(self.trade_history_csv, "a", newline="") as f:
            writer = csv.DictWriter(f, fieldnames=[
                "trade_id", "symbol", "direction", "entry_method",
                "entry_price", "stop_price", "target1", "target2",
                "qty_total", "opened_at", "closed_at", "exit_reason",
                "realized_pnl", "r_multiple",
            ])
            writer.writerow(row)

    def recent(self, n: int = 50) -> List[LogEvent]:
        return self.events[-n:][::-1]
