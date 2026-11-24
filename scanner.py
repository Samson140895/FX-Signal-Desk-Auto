
"""Live scanner: fresh graded signals, duplicate protection and paper journal."""
from __future__ import annotations

import json
import os

import pandas as pd

from . import DEFAULT_SYMBOLS
from . import journal
from .analysis import analyze, format_text
from .config import load as load_config
from .data import load_frames
from .modes import MODES
from .news import fetch_news, upcoming_high
from .notify import send
from .workflow import evaluate

BAR_MIN = {
    "1m": 1, "5m": 5, "15m": 15, "30m": 30,
    "1h": 60, "4h": 240, "1d": 1440
}
GRADE_RANK = {"A": 3, "B": 2, "C": 1, "D": 0}


def closed_only(df: pd.DataFrame, iv: str, now: pd.Timestamp) -> pd.DataFrame:
    """Drop the still-forming candle."""
    if df.index[-1] + pd.Timedelta(minutes=BAR_MIN[iv]) > now:
        return df.iloc[:-1]
    return df


def _utc(value) -> pd.Timestamp:
    t = pd.Timestamp(value)
    return t.tz_localize("UTC") if t.tzinfo is None else t.tz_convert("UTC")


def scan(
    symbols=None,
    modes=None,
    notify=True,
    min_grade="B",
    state_path=".state/seen.json",
) -> list[dict]:
    symbols = symbols or DEFAULT_SYMBOLS
    modes = modes or list(MODES)

    if os.path.exists(state_path):
        with open(state_path, encoding="utf-8") as f:
            seen = set(json.load(f))
    else:
        seen = set()

    now = pd.Timestamp.now(tz="UTC")
    fresh = []

    cfg = load_config()
    wf = cfg["workflow"]

    need = (
        {MODES[m]["interval"] for m in modes}
        | {MODES[m]["htf"] for m in modes if MODES[m]["htf"]}
    )

    if "scalp" in modes:
        need |= {wf["plan"], wf["setup"], wf["execute"]}
        if wf["use_precision"]:
            need.add(wf["precision"])

    frames = load_frames(symbols, sorted(need))

    # All journal checks use closed candles only.
    closed_frames = {
        sym: {
            tf: closed_only(df, tf, now)
            for tf, df in frames.get(sym, {}).items()
            if df is not None and not df.empty
        }
        for sym in symbols
    }

    # Update previously recorded paper trades.
    try:
        completed = journal.update(closed_frames)
        for trade in completed:
            status = trade["status"]
            if status == "TP2":
                message = f"🏁 JOURNAL: {trade['symbol']} {trade['side']} — TP2 reached"
            elif status == "TP1_BE":
                message = f"✅ JOURNAL: {trade['symbol']} {trade['side']} — TP1 reached, remainder closed at breakeven"
            else:
                message = f"❌ JOURNAL: {trade['symbol']} {trade['side']} — SL reached"

            (send if notify else print)(message)

    except Exception as e:
        print(f"[journal] update error: {e}")

    news = fetch_news()
    events = news["events"] if news.get("ok") else []

    for sym in symbols:
        for mode in modes:
            m = MODES[mode]

            if mode == "scalp":
                try:
                    fr = closed_frames.get(sym, {})
                    r = evaluate(sym, fr, cfg, events, now)
                except Exception as e:
                    print(f"[scan] {sym} workflow: {e}")
                    continue

                sg = r["signal"]
                if not sg:
                    continue

                signal_time = _utc(sg["timestamp"])
                interval = int(sg["exec_min"])
                monitor_from = signal_time + pd.Timedelta(minutes=interval)
                age = now - monitor_from

                # Do not send an expired workflow signal.
                if age < pd.Timedelta(0) or age > pd.Timedelta(minutes=15):
                    continue

                key = (
                    f"{sym}|workflow|{sg['strategy_key']}|"
                    f"{sg['direction']}|{sg['entry_zone']}"
                )

                if key in seen:
                    continue

                tp = sg["take_profit"]
                record = {
                    "id": key,
                    "symbol": sym,
                    "interval": wf["execute"],
                    "side": sg["direction"],
                    "entry": float(sg["entry"]),
                    "sl": float(sg["stop_loss"]),
                    "tp1": float(tp[0]),
                    "tp2": float(tp[1]),
                    "monitor_from": monitor_from.isoformat(),
                    "strategy": sg["strategy"],
                }

                try:
                    journal.add_signal(record)
                except Exception as e:
                    print(f"[journal] {sym} workflow record error: {e}")

                seen.add(key)
                fresh.append(r)
                (send if notify else print)(format_workflow(r))
                continue

            try:
                df = frames.get(sym, {}).get(m["interval"])
                if df is None or df.empty:
                    continue

                df = closed_frames.get(sym, {}).get(m["interval"])
                if df is None or df.empty:
                    continue

                htf = None
                if m["htf"]:
                    htf = closed_frames.get(sym, {}).get(m["htf"])

                an = analyze(sym, mode, df, htf, m["htf_name"])

            except Exception as e:
                print(f"[scan] {sym} {mode}: {e}")
                continue

            s = an["signal"]
            if not s:
                continue

            signal_open = _utc(s["time"])
            signal_close = signal_open + pd.Timedelta(
                minutes=BAR_MIN[m["interval"]]
            )
            age = now - signal_close

            entry_type = str(s.get("entry_type", "market")).lower()
            valid_minutes = (
                15 if entry_type == "market"
                else max(int(s.get("expiry", 1)), 1) * BAR_MIN[m["interval"]]
            )

            if age < pd.Timedelta(0) or age > pd.Timedelta(minutes=valid_minutes):
                continue

            if GRADE_RANK[s["grade"]] < GRADE_RANK[min_grade]:
                continue

            key = f"{sym}|{mode}|{s['strategy']}|{s['side']}|{s['time']}"
            if key in seen:
                continue

            record = {
                "id": key,
                "symbol": sym,
                "interval": m["interval"],
                "side": s["side"],
                "entry": float(s["entry"]),
                "sl": float(s["sl"]),
                "tp1": float(s["tp1"]),
                "tp2": float(s["tp2"]),
                "monitor_from": signal_close.isoformat(),
                "strategy": s["strategy"],
            }

            try:
                journal.add_signal(record)
            except Exception as e:
                print(f"[journal] {sym} {mode} record error: {e}")

            seen.add(key)
            fresh.append(an)

            text = format_text(an)
            warn = upcoming_high(events, sym, now)
            if warn:
                text += "\n" + warn

            (send if notify else print)(text)

    os.makedirs(os.path.dirname(state_path) or ".", exist_ok=True)
    with open(state_path, "w", encoding="utf-8") as f:
        json.dump(sorted(seen)[-500:], f)

    if not fresh:
        print(
            f"[scan] {now:%Y-%m-%d %H:%M} UTC - "
            f"checked {len(symbols)} pairs, no new graded setups"
        )

    stats = journal.statistics()
    print(
        "[journal] "
        f"total={stats['total']} open={stats['open']} "
        f"TP2={stats['TP2']} TP1_BE={stats['TP1_BE']} "
        f"SL={stats['SL']} win_rate={stats['win_rate']}%"
    )

    return fresh


def format_workflow(r: dict) -> str:
    """Telegram card for a final BUY / SELL from the multi-timeframe workflow."""
    from .alerts import from_workflow
    return from_workflow(r)
    
