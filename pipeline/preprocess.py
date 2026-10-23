#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-only
"""阶段一：把小说文本切成 章 / 场景，保留字符偏移与源哈希。

只做机械切分，不做文学判断：归属不确定就写 unknown，不硬猜。
输出 JSONL，一条一行，后续所有引文都靠 scene_id + 偏移回查。

用法:
    python pipeline/preprocess.py --work reverse_butterfly --input corpus/raw/reverse_butterfly.txt
    python pipeline/preprocess.py --work X --input a.txt --max-chars 3000 --out data/x.scenes.jsonl
"""
from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
MANIFEST = ROOT / "corpus" / "manifest.json"

CHAPTER_RE = re.compile(
    r"^\s*(?:第[一二三四五六七八九十百零〇0-9]{1,4}[章話话節节卷幕回][^\n]{0,80}"
    r"|CHAPTER\s+[0-9IVX]+[^\n]{0,60}|Chapter\s+\d+[^\n]{0,60}|##\s+\S[^\n]{0,80})",
    re.MULTILINE,
)
SEPARATOR_RE = re.compile(r"^\s*(?:\*{3,}|-{3,}|={3,}|[◆◇※＊*]{1,3}|○{2,})\s*$")
JUMP_RE = re.compile(
    r"^\s*(?:数分後|数時間後|翌日|翌朝|その夜|その頃|一方|同時刻|数日後|翌週|"
    r"几分钟后|几小时后|第二天|翌日|与此同时|另一方面|同一时刻|几天后|一周后|回到|镜头)"
)
NAME_HINTS = ("紅莉栖", "红莉栖", "牧瀬", "牧濑", "Kurisu", "助手", "クリス")


def utf8_console() -> None:
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass


def read_text(path: Path) -> str:
    raw = path.read_bytes()
    for enc in ("utf-8-sig", "utf-8", "utf-16", "cp932", "gb18030"):
        try:
            text = raw.decode(enc)
            break
        except UnicodeDecodeError:
            continue
    else:
        raise SystemExit(f"无法解码 {path}，请先转成 UTF-8")
    return text.replace("\r\n", "\n").replace("\r", "\n")


def load_manifest() -> dict:
    if not MANIFEST.exists():
        return {"works": []}
    return json.loads(MANIFEST.read_text("utf-8"))


def work_meta(manifest: dict, work: str) -> dict:
    for w in manifest.get("works", []):
        if w["work_id"] == work:
            return w
    return {"work_id": work, "tier": "unknown", "title_ja": work}


def chapter_spans(text: str) -> list[tuple[int, int, str]]:
    """返回 [(start, end, heading)]。没有章节标记时整篇作为第 1 章。"""
    matches = list(CHAPTER_RE.finditer(text))
    if not matches:
        return [(0, len(text), "")]
    spans = []
    for i, m in enumerate(matches):
        raw = m.group(0)
        start = m.start() + (len(raw) - len(raw.lstrip()))
        end = matches[i + 1].start() if i + 1 < len(matches) else len(text)
        spans.append((start, end, m.group(0).strip()))
    if matches[0].start() > 0 and text[: matches[0].start()].strip():
        spans.insert(0, (0, matches[0].start(), ""))
    return spans


def split_blocks(text: str, start: int, end: int) -> list[tuple[int, int]]:
    blocks, pos = [], start
    for m in re.finditer(r"\n\s*\n", text[start:end]):
        cut = start + m.start()
        if cut > pos:
            blocks.append((pos, cut))
        pos = start + m.end()
    if pos < end:
        blocks.append((pos, end))
    return [(a, b) for a, b in blocks if text[a:b].strip()]


def attribute(text: str) -> dict:
    """说话人归属：只给出证据强度，不假装确定。"""
    mentions = {name: text.count(name) for name in NAME_HINTS if name in text}
    quote_lines = [ln for ln in text.split("\n") if ("「" in ln or "『" in ln)]
    explicit = sum(1 for ln in quote_lines if any(name in ln for name in name_hints_of(text)))
    if not mentions:
        confidence = "unknown"
    elif explicit and explicit >= max(1, len(quote_lines) // 2):
        confidence = "explicit"
    else:
        confidence = "inferred"
    return {"mentions": mentions, "dialogue_lines": len(quote_lines), "confidence": confidence}


def name_hints_of(text: str) -> tuple[str, ...]:
    return tuple(n for n in NAME_HINTS if n in text)


def build_scenes(text: str, work: str, meta: dict, digest: str, max_chars: int) -> tuple[list[dict], list[str]]:
    warnings: list[str] = []
    scenes: list[dict] = []
    for ci, (c_start, c_end, heading) in enumerate(chapter_spans(text), start=1):
        blocks = split_blocks(text, c_start, c_end)
        current: list[tuple[int, int]] = []

        def flush() -> None:
            if not current:
                return
            s, e = current[0][0], current[-1][1]
            body = text[s:e]
            if body.strip():
                sid = f"{work}/c{ci:02d}/s{len([x for x in scenes if x['chapter'] == ci]) + 1:02d}"
                scenes.append({
                    "scene_id": sid,
                    "work": work,
                    "tier": meta.get("tier", "unknown"),
                    "title": meta.get("title_ja", work),
                    "chapter": ci,
                    "chapter_heading": heading,
                    "scene": len([x for x in scenes if x["chapter"] == ci]) + 1,
                    "start": s,
                    "end": e,
                    "sha256": digest,
                    "chars": e - s,
                    "text": body,
                    "attribution": attribute(body),
                })
            current.clear()

        for b_start, b_end in blocks:
            body = text[b_start:b_end]
            if SEPARATOR_RE.match(body):
                flush()
                continue
            if JUMP_RE.match(body) and current and (current[-1][1] - current[0][0]) > max_chars // 3:
                flush()
            current.append((b_start, b_end))
            if (current[-1][1] - current[0][0]) >= max_chars:
                flush()
        flush()
    if not scenes:
        warnings.append("未切出任何场景：检查编码与章节标记")
    return scenes, warnings


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--work", required=True, help="work_id，需与 corpus/manifest.json 一致")
    ap.add_argument("--input", type=Path, required=True)
    ap.add_argument("--out", type=Path, default=None)
    ap.add_argument("--max-chars", type=int, default=4000)
    ap.add_argument("--tier", default=None, help="覆盖 manifest 中的 tier，仅用于临时试跑")
    args = ap.parse_args()
    utf8_console()

    text = read_text(args.input)
    digest = hashlib.sha256(args.input.read_bytes()).hexdigest()
    meta = work_meta(load_manifest(), args.work)
    if args.tier:
        meta = dict(meta, tier=args.tier)

    scenes, warnings = build_scenes(text, args.work, meta, digest, args.max_chars)
    out = args.out or (ROOT / "data" / f"{args.work}.scenes.jsonl")
    out.parent.mkdir(parents=True, exist_ok=True)
    with out.open("w", encoding="utf-8", newline="\n") as fh:
        for sc in scenes:
            fh.write(json.dumps(sc, ensure_ascii=False) + "\n")

    chars = sum(s["chars"] for s in scenes)
    conf = {}
    for s in scenes:
        conf[s["attribution"]["confidence"]] = conf.get(s["attribution"]["confidence"], 0) + 1
    print(f"源文件 {args.input}  sha256={digest[:16]}…  {len(text)} 字符  编码可读")
    print(f"切出 {len(scenes)} 个场景 / {len({s['chapter'] for s in scenes})} 章，覆盖 {chars} 字符")
    print(f"说话人归属置信度分布: {conf}  （unknown 不算失败，硬猜才是）")
    print(f"输出: {out}")
    for w in warnings:
        print("WARN", w)
    return 0 if scenes else 2


if __name__ == "__main__":
    raise SystemExit(main())
