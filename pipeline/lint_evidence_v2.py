#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-only
"""证据库 v2 溯源校验。

与 v1 的差别：scene_id 改为稳定 UID（AR-V1-S001），增加 evidence_role、
speaker、source_location、review_status。引文仍逐字回查原文。

用法:
    python pipeline/lint_evidence_v2.py --evidence data/evidence_database.jsonl
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from collections import Counter, defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
MODEL = ROOT / "persona" / "kurisu_persona_model.json"
DATA = ROOT / "data"

ID_RE = re.compile(r"^(?:AR|LM|MOV)(?:-[A-Z0-9]+)?-S\d{3}\.\d{3}$")
TYPES = {"inner_thought", "dialogue", "action", "decision", "narrator", "other_evaluation"}
ROLES = {"supports", "contradicts", "limits", "context", "neutral"}
STATUS = {"verified", "pending", "rejected"}
CONF = {"high", "medium", "low"}
REQUIRED = ("evidence_id", "scene_id", "work", "volume", "chapter", "source_location", "speaker",
            "evidence_type", "original_quote", "observable_behavior", "candidate_mechanism",
            "evidence_role", "interpretation", "confidence", "review_status")


def read_jsonl(path: Path) -> list[dict]:
    return [json.loads(line) for line in path.read_text("utf-8").splitlines() if line.strip()]


def model_ids(model: dict) -> set[str]:
    ids = {i["id"] for i in model["core_traits"]}
    ids |= {i["id"] for i in model["cognitive_rules"]}
    ids |= {i["id"] for i in model["values"]["items"]}
    ids |= {i["id"] for i in model["emotion_triggers"]}
    ids |= {i["id"] for i in model["social_model"]["stages"]}
    ids.add(model["social_model"]["conflict_repair"]["id"])
    ids |= {i["id"] for i in model["language_model"]["features"]}
    ids |= {i["id"] for i in model["private_interest"]}
    ids |= {i["id"] for i in model["privacy_boundary"]}
    ids |= {i["id"] for i in model["self_image"]}
    return ids


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--evidence", type=Path, required=True)
    ap.add_argument("--fix", action="store_true")
    ap.add_argument("--report", type=Path, help="写出机器可读的校验报告（v2 口径）")
    args = ap.parse_args()
    sys.stdout.reconfigure(encoding="utf-8")

    model = json.loads(MODEL.read_text("utf-8"))
    known = model_ids(model)
    scenes = {r["uid"]: r for r in read_jsonl(DATA / "scene_index.jsonl")}
    texts = {}
    raw_texts: dict[str, str] = {}
    for work in ("assistant_persona", "assistant_perspective", "movie_novel", "linear_bounded"):
        path = DATA / f"{work}.scenes.jsonl"
        if not path.exists():
            continue
        for s in read_jsonl(path):
            texts[s["scene_id"]] = s["text"]

    def raw_text(work: str) -> str:
        """包内不含 scenes 时，退回 corpus/raw 全文回查（只证明引文存在，不证明场景归属）。"""
        if work in raw_texts:
            return raw_texts[work]
        path = ROOT / "corpus" / "raw" / f"{work}.txt"
        text = ""
        if path.exists():
            blob = path.read_bytes()
            for enc in ("utf-8-sig", "utf-8", "gb18030"):
                try:
                    text = blob.decode(enc)
                    break
                except UnicodeDecodeError:
                    continue
        raw_texts[work] = text
        return text

    rows = read_jsonl(args.evidence)
    errors, warnings = [], []
    fallback_seen: set[str] = set()
    seen = set()
    for r in rows:
        eid = r.get("evidence_id", "?")
        for k in REQUIRED:
            if r.get(k) in (None, "", []):
                errors.append((eid, f"缺少 {k}"))
        if not ID_RE.match(str(eid)):
            errors.append((eid, "evidence_id 格式应为 <UID>.<NNN>"))
        if eid in seen:
            errors.append((eid, "evidence_id 重复"))
        seen.add(eid)

        scene = scenes.get(r.get("scene_id", ""))
        if scene is None:
            errors.append((eid, f"UID 不在索引中: {r.get('scene_id')}"))
            continue
        if r.get("work") != scene["work"] or r.get("volume") != scene["volume"]:
            errors.append((eid, "work/volume 与索引不一致"))
        if r.get("chapter") != scene["chapter"]:
            errors.append((eid, "chapter 与索引不一致"))

        if r.get("evidence_type") not in TYPES:
            errors.append((eid, f"非法 evidence_type: {r.get('evidence_type')}"))
        if r.get("evidence_role") not in ROLES:
            errors.append((eid, f"非法 evidence_role: {r.get('evidence_role')}"))
        if r.get("review_status") not in STATUS:
            errors.append((eid, f"非法 review_status: {r.get('review_status')}"))
        if r.get("confidence") not in CONF:
            errors.append((eid, f"非法 confidence: {r.get('confidence')}"))
        if r.get("candidate_mechanism") not in known:
            errors.append((eid, f"机制不存在: {r.get('candidate_mechanism')}"))
        if r.get("evidence_type") == "other_evaluation" and r.get("confidence") == "high":
            errors.append((eid, "他人评价不得 high"))
        if (r.get("alternative_explanation") or []) and r.get("confidence") == "high":
            errors.append((eid, "有替代解释时不得 high"))

        quote = r.get("original_quote")
        if quote:
            if len(quote) > 60:
                errors.append((eid, f"引文 {len(quote)} 字，超过 60"))
            text = texts.get(scene["legacy_scene_id"], "")
            if not text:
                fallback = raw_text(scene["work"])
                if fallback:
                    text = fallback
                    if scene["work"] not in fallback_seen:
                        fallback_seen.add(scene["work"])
                        warnings.append((scene["work"], "包内无 scenes：退回 corpus/raw 全文回查（未按场景核对）"))
            if quote not in text:
                squeezed = re.sub(r"\s+", "", text)
                if re.sub(r"\s+", "", quote) in squeezed:
                    warnings.append((eid, "仅空白归一化后匹配"))
                else:
                    errors.append((eid, "引文不是原文子串"))

    counts = Counter(r["candidate_mechanism"] for r in rows if r["review_status"] == "verified")
    roles = Counter(r["evidence_role"] for r in rows if r["review_status"] == "verified")
    print(f"证据 {len(rows)} 条 ｜ 可用 {sum(1 for r in rows if r['review_status'] == 'verified')} ｜ 分层 {dict(Counter(r['work'] for r in rows if r['review_status'] == 'verified'))}")
    print(f"角色分布: {dict(roles)}")
    print(f"机制覆盖: {len([k for k in counts if counts[k] > 0])} / {len(known)}")
    for eid, msg in warnings:
        print(f"WARN {eid}: {msg}")
    for eid, msg in errors:
        print(f"FAIL {eid}: {msg}", file=sys.stderr)
    print(f"\n结论: {'通过' if not errors else '不通过'}（{len(errors)} 错 / {len(warnings)} 警）")

    if args.report:
        verified = [r for r in rows if r["review_status"] == "verified"]
        report = {
            "schema": "kurisu-persona/lint-report-v2",
            "evidence_file": args.evidence.as_posix(),
            "evidence_total": len(rows),
            "usable": len(verified),
            "rejected": sum(1 for r in rows if r["review_status"] == "rejected"),
            "pending": sum(1 for r in rows if r["review_status"] == "pending"),
            "by_work": dict(Counter(r["work"] for r in verified)),
            "by_role": dict(Counter(r["evidence_role"] for r in verified)),
            "by_type": dict(Counter(r["evidence_type"] for r in verified)),
            "by_confidence": dict(Counter(r["confidence"] for r in verified)),
            "by_chapter": dict(sorted(Counter("%s/ch%02d" % (r["work"], r["chapter"]) for r in verified).items())),
            "mechanisms": dict(sorted(Counter(r["candidate_mechanism"] for r in verified).items())),
            "scenes": len({r["scene_id"] for r in verified}),
            "mechanisms_covered": len(counts),
            "mechanisms_known": len(known),
            "errors": [{"evidence_id": e, "message": m} for e, m in errors],
            "warnings": [{"evidence_id": e, "message": m} for e, m in warnings],
            "conclusion": "pass" if not errors else "fail",
        }
        args.report.parent.mkdir(parents=True, exist_ok=True)
        args.report.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n",
                               encoding="utf-8", newline="\n")
        print(f"报告已写入 {args.report}")
    return 1 if errors else 0


if __name__ == "__main__":
    raise SystemExit(main())
