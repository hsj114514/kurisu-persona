#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-only
"""v2 聚合：把证据角色（supports/limits/contradicts）折算为机制状态。

规则（比 v1 更保守）：
- supported 需要：core 层至少 3 个独立场景支持，且全部作品合计至少 4 个
- weak：core 支持 >=1，或任意作品支持 >=2
- contradicted：contradicts 场景数 >= 2 且 core 支持 < 3
- limits 不会提升状态；若某机制 limits 场景数 >= 支持场景数，最多为 weak
- 只用 review_status=verified 的行

用法:
    python pipeline/aggregate_v2.py --report
    python pipeline/aggregate_v2.py --write
"""
from __future__ import annotations

import argparse
import json
import shutil
import sys
import time
from collections import Counter, defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DATA = ROOT / "data"
MODEL = ROOT / "persona" / "kurisu_persona_model.json"


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--evidence", type=Path, default=DATA / "evidence_database.jsonl")
    ap.add_argument("--write", action="store_true")
    ap.add_argument("--report", action="store_true")
    args = ap.parse_args()
    sys.stdout.reconfigure(encoding="utf-8")

    rows = [r for r in (json.loads(l) for l in args.evidence.read_text("utf-8").splitlines() if l.strip())
            if r.get("review_status") == "verified"]
    model = json.loads(MODEL.read_text("utf-8"))

    by_mech = defaultdict(list)
    for r in rows:
        by_mech[r["candidate_mechanism"]].append(r)

    items = []
    for g in ("core_traits", "cognitive_rules"):
        items += model[g]
    items += model["values"]["items"] + model["emotion_triggers"] + model["social_model"]["stages"] + model["private_interest"] + model["privacy_boundary"] + model["self_image"]
    items += [model["social_model"]["conflict_repair"]] + model["language_model"]["features"]

    changes = []
    for item in items:
        rs = by_mech.get(item["id"], [])
        sup = [r for r in rs if r["evidence_role"] == "supports"]
        lim = [r for r in rs if r["evidence_role"] == "limits"]
        con = [r for r in rs if r["evidence_role"] == "contradicts"]
        sup_core = {r["scene_id"] for r in sup if r["work"] == "assistant_perspective"}
        sup_all = {r["scene_id"] for r in sup}
        lim_scenes = {r["scene_id"] for r in lim}
        con_scenes = {r["scene_id"] for r in con}
        ctx = [r for r in rs if r["evidence_role"] == "context"]
        item["evidence_stats"] = {
            "supports": len(sup),
            "limits": len(lim),
            "contradicts": len(con),
            "context": len(ctx),
            "scenes": len(sup_all),
            "scenes_core": len(sup_core),
        }

        if len(con_scenes) >= 2 and len(sup_core) < 3:
            new = "contradicted"
        elif len(sup_core) >= 3 and len(sup_all) >= 4:
            new = "supported"
        elif sup_core or len(sup_all) >= 2:
            new = "weak"
        elif rs:
            new = "contradicted" if con else "weak"
        else:
            new = "prior"

        if new in ("supported", "weak") and len(lim_scenes) >= len(sup_all) and sup_all:
            new = "weak"

        old = item["status"]
        if new != "prior" or rs:
            item["status"] = new
            item["evidence_ids"] = sorted(r["evidence_id"] for r in rs)
            item["requires_verification"] = new != "supported"
            note = item.get("note", "")
            # 自动追加部分每次重算，不在旧值上叠加（否则重跑会重复长串场景列表）
            for marker in ("；存在反证场景：", "；适用边界："):
                if marker in note:
                    note = note.split(marker)[0]
            if con_scenes:
                extra = "存在反证场景：" + "、".join(sorted(con_scenes))[:200]
                note = (note + "；" + extra).strip("；")
            if lim_scenes:
                extra = "适用边界：" + "、".join(sorted(lim_scenes))[:200]
                note = (note + "；" + extra).strip("；")
            if note:
                item["note"] = note
        if old != new:
            changes.append((item["id"], old, new, len(sup_core), len(sup_all), len(lim_scenes), len(con_scenes)))

    tiers = Counter(r["work"] for r in rows)
    statuses = Counter(i["status"] for i in items)
    gaps = []
    for i in items:
        if i["status"] == "prior":
            gaps.append(f"{i['id']} 尚无可用支持证据")
    if not tiers.get("assistant_perspective"):
        gaps.append("core 层无证据")
    model["coverage"] = {
        "evidence_total": len(rows),
        "by_tier": {"core": tiers.get("assistant_perspective", 0),
                    "aux": tiers.get("movie_novel", 0),
                    "derivative": tiers.get("linear_bounded", 0)},
        "by_status": {k: statuses.get(k, 0) for k in ("prior", "weak", "supported", "contradicted")},
        "gaps": gaps,
    }
    model["generated_at"] = time.strftime("%Y-%m-%d")

    if args.report or not args.write:
        print(f"可用证据 {len(rows)} 条 / 独立场景 {len({r['scene_id'] for r in rows})}")
        print(f"{'id':<5}{'旧':<14}{'新':<14}{'core':>5}{'all':>4}{'lim':>4}{'con':>4}")
        for c in changes:
            print(f"{c[0]:<5}{c[1]:<14}{c[2]:<14}{c[3]:>5}{c[4]:>4}{c[5]:>4}{c[6]:>4}")
        if not changes:
            print("（无状态变化）")
        print(f"\n状态分布: {dict(statuses)}")
        print(f"分层证据: {dict(tiers)}")

    if args.write:
        if MODEL.exists():
            shutil.copy2(MODEL, MODEL.with_suffix(".json.bak"))
        MODEL.write_text(json.dumps(model, ensure_ascii=False, indent=2) + "\n", encoding="utf-8", newline="\n")
        print(f"已写回 {MODEL}（备份 .bak）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
