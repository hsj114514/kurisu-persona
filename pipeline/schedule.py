#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-only
"""从本地互动时间学习活跃时段（只在 schedule 授权开启时使用）。

口径：
- 只统计 `local`（或未标主体）的用户消息，按本地时区的小时归档；
- 少于 30 条或少于 5 个不同日期 → `insufficient`，不生成结论；
- 活跃小时 = 计数 >= max(2, 5% 总样本)；
- 连续活跃小时合并为窗口；其余为安静窗口；
- 输出 `state/schedule.json`，不写回人格或证据。

用法:
    python pipeline/schedule.py --show
    python pipeline/schedule.py --write
"""
from __future__ import annotations

import argparse
import json
import sys
from collections import Counter
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
INTERACTION = ROOT / "memory" / "interaction.jsonl"
SESSION = ROOT / "memory" / "session.jsonl"
OUT = ROOT / "state" / "schedule.json"
MIN_MESSAGES = 30
MIN_DAYS = 5
CONSENT = ROOT / "memory" / "consent.json"
RULES = ROOT / "state" / "rules.json"


def read_jsonl(path: Path) -> list[dict]:
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text("utf-8").splitlines() if line.strip()]


def schedule_allowed(consent_path: Path = CONSENT, rules_path: Path = RULES) -> tuple[bool, str]:
    if consent_path.exists():
        try:
            data = json.loads(consent_path.read_text("utf-8"))
        except ValueError:
            return False, "consent.json 无法解析"
        if not isinstance(data, dict) or not isinstance(data.get("subjects", {}), dict):
            return False, "授权结构不合法"
        rec = (data.get("subjects") or {}).get("local") or {}
        if not isinstance(rec, dict) or not isinstance(rec.get("scopes", {}), dict):
            return False, "授权结构不合法"
        value = (rec.get("scopes") or {}).get("schedule")
        if isinstance(value, bool):
            return value, "memory/consent.json"
        if isinstance(value, dict) and isinstance(value.get("granted"), bool):
            return value["granted"], "memory/consent.json"
        return False, "consent.json 缺少 local.schedule"
    try:
        rules = json.loads(rules_path.read_text("utf-8"))
        return (rules.get("consent") or {}).get("default", {}).get("schedule") is True, "rules.json 默认"
    except (OSError, ValueError, TypeError, AttributeError):
        return False, "读不到授权"


def samples(paths: list[Path], now: datetime | None = None) -> list[datetime]:
    now = now or datetime.now().astimezone()
    out, seen = [], set()
    for path in paths:
        for row in read_jsonl(path):
            if row.get("speaker") != "user":
                continue
            if row.get("subject") not in (None, "local"):
                continue
            ts = row.get("ts")
            if not ts:
                continue
            expires = row.get("expires_at")
            if expires:
                try:
                    expiry = datetime.fromisoformat(expires)
                    if expiry.tzinfo is None or expiry <= now:
                        continue
                except (ValueError, TypeError):
                    continue
            try:
                stamp = datetime.fromisoformat(ts)
            except ValueError:
                continue
            if stamp.tzinfo is None or stamp.utcoffset() is None or stamp > now:
                continue
            stamp = stamp.astimezone()
            key = (stamp.isoformat(), (row.get("text") or "")[:40])
            if key in seen:
                continue
            seen.add(key)
            out.append(stamp)
    return out


def merge_hours(hours: list[int]) -> list[list[int]]:
    if not hours:
        return []
    hours = sorted(set(hours))
    windows = [[hours[0], hours[0] + 1]]
    for hour in hours[1:]:
        if hour == windows[-1][1]:
            windows[-1][1] = hour + 1
        else:
            windows.append([hour, hour + 1])
    return windows


def complement(windows: list[list[int]]) -> list[list[int]]:
    out, cursor = [], 0
    for start, end in windows:
        if start > cursor:
            out.append([cursor, start])
        cursor = max(cursor, end)
    if cursor < 24:
        out.append([cursor, 24])
    return out


def learn(paths: list[Path] | None = None, *, consent_path: Path = CONSENT,
          rules_path: Path = RULES, now: datetime | None = None) -> dict:
    allowed, why = schedule_allowed(consent_path, rules_path)
    if not allowed:
        return {"state": "unauthorized", "reason": f"schedule 未授权（{why}）"}
    stamps = samples(paths or [INTERACTION, SESSION], now)
    days = {s.date().isoformat() for s in stamps}
    if len(stamps) < MIN_MESSAGES or len(days) < MIN_DAYS:
        return {
            "state": "insufficient",
            "messages": len(stamps),
            "days": len(days),
            "need_messages": MIN_MESSAGES,
            "need_days": MIN_DAYS,
        }
    counts = Counter(s.hour for s in stamps)
    threshold = max(2, int(len(stamps) * 0.05))
    active = [hour for hour, n in counts.items() if n >= threshold]
    windows = merge_hours(active)
    quiet = complement(windows)
    confidence = round(min(1.0, len(stamps) / 200), 2)
    return {
        "state": "ok",
        "generated_at": (now or datetime.now().astimezone()).isoformat(timespec="seconds"),
        "messages": len(stamps),
        "days": len(days),
        "threshold": threshold,
        "hour_counts": {str(h): counts.get(h, 0) for h in range(24)},
        "active_hours": sorted(active),
        "active_windows": windows,
        "quiet_windows": quiet,
        "confidence": confidence,
    }


def fmt_windows(windows: list[list[int]]) -> str:
    return "、".join(f"{a:02d}:00–{b:02d}:00" for a, b in windows)


def main() -> int:
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass
    ap = argparse.ArgumentParser()
    ap.add_argument("--show", action="store_true")
    ap.add_argument("--write", action="store_true")
    args = ap.parse_args()

    result = learn()
    if result["state"] == "unauthorized":
        print(result["reason"], file=sys.stderr)
        return 2
    if args.write:
        OUT.parent.mkdir(parents=True, exist_ok=True)
        OUT.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n",
                       encoding="utf-8", newline="\n")
        print(f"已写入 {OUT}")
    if result["state"] == "insufficient":
        print(f"样本不足：{result['messages']} 条 / {result['days']} 天"
              f"（需要 {MIN_MESSAGES} 条、{MIN_DAYS} 天）；不生成作息结论。")
        return 0
    print(f"样本 {result['messages']} 条 / {result['days']} 天；阈值 {result['threshold']}")
    print("活跃窗口：" + fmt_windows(result["active_windows"]))
    print("安静窗口：" + fmt_windows(result["quiet_windows"]))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
