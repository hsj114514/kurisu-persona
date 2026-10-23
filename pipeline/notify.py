#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-only
"""本地通知：写 JSONL 日志，并尝试 Windows 桌面通知（失败只降级，不阻塞）。

用法:
    python pipeline/notify.py "标题" "正文"
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
LOG = ROOT / "runtime" / "outreach" / "notifications.jsonl"
TOAST = ROOT / "tools" / "toast.ps1"


def notify(title: str, body: str, *, log: Path | None = None, toast: bool = True,
           timeout: int = 10) -> dict:
    log = log or LOG
    log.parent.mkdir(parents=True, exist_ok=True)
    record = {
        "ts": datetime.now().astimezone().isoformat(timespec="seconds"),
        "title": title,
        "body": body,
    }
    with log.open("a", encoding="utf-8", newline="\n") as fh:
        fh.write(json.dumps(record, ensure_ascii=False, sort_keys=True) + "\n")

    if toast and os.name == "nt" and TOAST.exists():
        try:
            with tempfile.TemporaryDirectory() as tmp:
                title_file = Path(tmp) / "title.txt"
                body_file = Path(tmp) / "body.txt"
                title_file.write_text(title, encoding="utf-8")
                body_file.write_text(body, encoding="utf-8")
                subprocess.run(
                    ["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", str(TOAST),
                     "-TitleFile", str(title_file), "-BodyFile", str(body_file)],
                    capture_output=True, timeout=timeout,
                )
        except (OSError, subprocess.SubprocessError):
            pass
    return record


def main() -> int:
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass
    if len(sys.argv) < 3:
        print("用法: python pipeline/notify.py 标题 正文", file=sys.stderr)
        return 2
    print(json.dumps(notify(sys.argv[1], sys.argv[2]), ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
