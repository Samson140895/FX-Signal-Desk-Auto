
"""Paper-trade journal: track TP1, TP2 and SL using closed candles."""
from __future__ import annotations

import json
import os
from datetime import datetime, timezone

import pandas as pd


def _save(path: str, trades: list[dict]) -> None:
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(trades, f, indent=2)
    os.replace(tmp, path)


def load(path: str = ".state/journal.json") -> list[dict]:
    if not os.path.exists(path):
        return []
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return []


def add_signal(record: dict, path: str = ".state/journal.json") -> bool:
    """Add a signal once. Required fields are supplied by the scanner."""
    trades = load(path)
    if any(t["id"] == record["id"] for t in trades):
        return False

    item = dict(record)
    item.update(
        status="OPEN",
        tp1_hit=False,
        created_at=datetime.now(timezone.utc).isoformat(),
        checked_until=None,
    )
    trades.append(item)
    _save(path, trades)
    return True


def update(frames: dict, path: str = ".state/journal.json") -> list[dict]:
    """Update open paper trades using CLOSED OHLC candles."""
    trades = load(path)
    changed = []

    for trade in trades:
        if trade["status"] != "OPEN":
            continue

        df = frames.get(trade["symbol"], {}).get(trade["interval"])
        if df is None or df.empty:
            continue

        start = pd.Timestamp(trade["monitor_from"])
        if start.tzinfo is None:
            start = start.tz_localize("UTC")

        checked = trade.get("checked_until")
        if checked:
            checked = pd.Timestamp(checked)
            if checked.tzinfo is None:
                checked = checked.tz_localize("UTC")

        candles = df[df.index >= start]
        if checked is not None:
            candles = candles[candles.index > checked]

        side = 1 if trade["side"] == "BUY" else -1
        entry = float(trade["entry"])
        sl = float(trade["sl"])
        tp1 = float(trade["tp1"])
        tp2 = float(trade["tp2"])

        for ts, candle in candles.iterrows():
            high, low = float(candle["high"]), float(candle["low"])

            # Before TP1: SL has priority if levels conflict in one candle.
            if not trade["tp1_hit"]:
                hit_sl = low <= sl if side == 1 else high >= sl
                hit_tp1 = high >= tp1 if side == 1 else low <= tp1

                if hit_sl:
                    trade["status"] = "SL"
                    trade["closed_at"] = ts.isoformat()
                    break

                if hit_tp1:
                    trade["tp1_hit"] = True
                    trade["tp1_at"] = ts.isoformat()
                    trade["sl"] = entry  # remaining position moves to breakeven
                    # Do not assume TP2 happened in the same candle.
                    continue

            else:
                # After TP1, the remaining position is protected at breakeven.
                hit_be = low <= entry if side == 1 else high >= entry
                hit_tp2 = high >= tp2 if side == 1 else low <= tp2

                if hit_be:
                    trade["status"] = "TP1_BE"
                    trade["closed_at"] = ts.isoformat()
                    break

                if hit_tp2:
                    trade["status"] = "TP2"
                    trade["closed_at"] = ts.isoformat()
                    break

        if not candles.empty:
            trade["checked_until"] = candles.index[-1].isoformat()

        if trade["status"] != "OPEN":
            changed.append(trade)

    _save(path, trades)
    return changed


def statistics(path: str = ".state/journal.json") -> dict:
    trades = load(path)
    closed = [t for t in trades if t["status"] != "OPEN"]
    counts = {
        status: sum(t["status"] == status for t in closed)
        for status in ("TP2", "TP1_BE", "SL")
    }
    wins = counts["TP2"] + counts["TP1_BE"]
    return {
        "total": len(trades),
        "open": sum(t["status"] == "OPEN" for t in trades),
        "closed": len(closed),
        **counts,
        "win_rate": round(wins / len(closed) * 100, 1) if closed else 0.0,
    }
