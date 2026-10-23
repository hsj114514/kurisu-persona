#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-only
"""阶段二：把场景变成「人格证据抽取」任务，可选直接调用 OpenAI 兼容端点。

默认不联网：先生成 tasks.jsonl（含完整 prompt），你可以用任何模型批量跑。
给了 --endpoint 才发请求，且只发送当前场景文本，不上传全文或整本小说。

用法:
    python pipeline/extract.py --scenes data/reverse_butterfly.scenes.jsonl --out data/tasks.jsonl
    python pipeline/extract.py --scenes ... --endpoint https://api.example.com --model my-model \
        --api-key-env MY_KEY --out data/evidence.raw.jsonl
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
MODEL = ROOT / "persona" / "kurisu_persona_model.json"

SYSTEM = """你是严谨的文学文本标注器，任务是为「角色人格建模」抽取证据，而不是写读后感。

铁律：
1. 只标注给出的这一段文本。不得引入其他章节、动画、游戏、其他小说的内容。
2. quote 必须是原文的**逐字子串**，不超过 60 字；找不到合适引文就填 null。系统会做字节级回查，编造会被整条拒收。
3. 区分三档：fact（文本直接证实）/ inference（多个细节合理推断）/ speculation（证据不足的猜测）。
   speculation 可以写，但会被系统忽略，不许伪装成 fact。
4. 他人评价（other_evaluation）只能作为旁证，不能单独支撑一条人格规则。
5. 同一表现若有别的解释，写进 alternative_explanations；这时 confidence 不得为 high。
6. 宁可返回空数组。这一段没有有价值的人格证据时，返回 {"evidence": []}，不要凑数。
7. 不要复述剧情梗概；不要评价文笔；不要输出任何解释性文字，只输出 JSON。"""

USER_TMPL = """【作品】{work}（{tier}）
【位置】第 {chapter} 章 / 第 {scene} 场
【场景原文】
<<<SCENE
{text}
SCENE>>>

【目标角色】{character}。别名提示：{aliases}
注意：文中其他角色对她的评价属于 other_evaluation，不等于她本人的人格。

【可用机制编号】（mechanism 只能填这些 id；填不出就说明这段不该抽）
{catalog}

【输出格式】严格 JSON：
{{"evidence": [
  {{
    "type": "dialogue|action|inner_thought|narrator|other_evaluation|decision",
    "speaker_certainty": "explicit|inferred|unknown",
    "quote": "原文逐字子串或 null",
    "context": "触发情境：发生了什么、谁在场、关系状态",
    "behavior": "她的实际表现",
    "mechanism": "T01 这类编号",
    "claim": "这条证据支持的一句话机制表述",
    "reasoning": "为什么这样解释，而不是别的解释",
    "content_kind": "fact|inference|speculation",
    "confidence": "high|medium|low",
    "contradicts": [],
    "alternative_explanations": []
  }}
]}}

只输出这个 JSON，不要 markdown 代码块，不要前后说明。"""


def utf8_console() -> None:
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass


def read_jsonl(path: Path) -> list[dict]:
    return [json.loads(line) for line in path.read_text("utf-8").splitlines() if line.strip()]


def catalog(model: dict) -> str:
    rows = []
    for item in model["core_traits"] + model["cognitive_rules"] + model["values"]["items"]:
        rows.append(f"{item['id']} = {item['statement'][:48]}")
    for item in model["emotion_triggers"]:
        rows.append(f"{item['id']} = 情绪触发：{item['trigger'][:40]}")
    for item in model["social_model"]["stages"]:
        rows.append(f"{item['id']} = 社交：{item['statement'][:40]}")
    for item in model["language_model"]["features"]:
        rows.append(f"{item['id']} = 语言：{item['statement'][:40]}")
    return "\n".join(rows)


def make_task(scene: dict, cat: str, character: str) -> dict:
    user = USER_TMPL.format(
        work=scene["work"],
        tier=scene.get("tier", "unknown"),
        chapter=scene["chapter"],
        scene=scene["scene"],
        text=scene["text"],
        character=character,
        aliases="、".join(["紅莉栖", "红莉栖", "牧瀬", "牧濑", "Kurisu"]),
        catalog=cat,
    )
    return {
        "scene_id": scene["scene_id"],
        "work": scene["work"],
        "tier": scene.get("tier"),
        "chapter": scene["chapter"],
        "scene": scene["scene"],
        "messages": [{"role": "system", "content": SYSTEM}, {"role": "user", "content": user}],
    }


def convert(result: dict, task: dict, seq_start: int) -> list[dict]:
    """把模型输出转成 evidence 记录；这里只做结构清洗，不做内容背书。"""
    out = []
    for i, ev in enumerate(result.get("evidence") or [], start=seq_start):
        work = task["work"]
        out.append({
            "evidence_id": f"{work}.c{task['chapter']:02d}s{task['scene']:02d}.{i:03d}",
            "scene_id": task["scene_id"],
            "work": work,
            "tier": task["tier"],
            "chapter": task["chapter"],
            "scene": task["scene"],
            "char_start": None,
            "char_end": None,
            "quote": ev.get("quote"),
            "type": ev.get("type", "dialogue"),
            "speaker_certainty": ev.get("speaker_certainty", "unknown"),
            "context": ev.get("context", ""),
            "behavior": ev.get("behavior", ""),
            "mechanism": ev.get("mechanism", ""),
            "claim": ev.get("claim", ""),
            "reasoning": ev.get("reasoning", ""),
            "content_kind": ev.get("content_kind", "inference"),
            "confidence": ev.get("confidence", "low"),
            "status": "draft",
            "contradicts": ev.get("contradicts") or [],
            "alternative_explanations": ev.get("alternative_explanations") or [],
        })
    return out


def call_endpoint(endpoint: str, model: str, api_key: str, messages: list[dict], timeout: int = 120) -> dict:
    body = json.dumps({
        "model": model,
        "messages": messages,
        "temperature": 0.1,
        "response_format": {"type": "json_object"},
    }).encode("utf-8")
    req = urllib.request.Request(
        endpoint.rstrip("/") + "/chat/completions",
        data=body,
        headers={"Content-Type": "application/json", "Authorization": f"Bearer {api_key}"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        payload = json.loads(resp.read().decode("utf-8"))
    content = payload["choices"][0]["message"]["content"]
    content = content.strip().removeprefix("```json").removeprefix("```").removesuffix("```").strip()
    return json.loads(content)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--scenes", type=Path, required=True)
    ap.add_argument("--out", type=Path, required=True)
    ap.add_argument("--endpoint", default=None, help="OpenAI 兼容 base URL；不填则只生成任务")
    ap.add_argument("--model", default=None)
    ap.add_argument("--api-key-env", default="OPENAI_API_KEY")
    ap.add_argument("--limit", type=int, default=0, help="只处理前 N 个场景（0=全部）")
    ap.add_argument("--min-chars", type=int, default=200, help="跳过过短的场景")
    ap.add_argument("--sleep", type=float, default=0.0)
    args = ap.parse_args()
    utf8_console()

    model = json.loads(MODEL.read_text("utf-8"))
    cat = catalog(model)
    scenes = read_jsonl(args.scenes)
    scenes = [s for s in scenes if s["chars"] >= args.min_chars]
    if args.limit:
        scenes = scenes[: args.limit]
    tasks = [make_task(s, cat, model["character"]) for s in scenes]

    args.out.parent.mkdir(parents=True, exist_ok=True)
    if not args.endpoint:
        with args.out.open("w", encoding="utf-8", newline="\n") as fh:
            for task in tasks:
                fh.write(json.dumps(task, ensure_ascii=False) + "\n")
        chars = sum(len(t["messages"][1]["content"]) for t in tasks)
        print(f"已生成 {len(tasks)} 个抽取任务 -> {args.out}")
        print(f"输入约 {chars} 字符（≈{chars // 3} tokens），未发任何网络请求。")
        print("用你的模型跑 tasks.jsonl，把输出交回 lint_evidence.py 校验。")
        return 0

    api_key = os.environ.get(args.api_key_env, "")
    if not api_key:
        print(f"缺少 API key：请设置环境变量 {args.api_key_env}", file=sys.stderr)
        return 2
    if not args.model:
        print("--endpoint 需要同时给 --model", file=sys.stderr)
        return 2

    total, errors = 0, 0
    with args.out.open("w", encoding="utf-8", newline="\n") as fh:
        for idx, task in enumerate(tasks, start=1):
            try:
                result = call_endpoint(args.endpoint, args.model, api_key, task["messages"])
                rows = convert(result, task, 1)
                for row in rows:
                    fh.write(json.dumps(row, ensure_ascii=False) + "\n")
                total += len(rows)
                print(f"[{idx}/{len(tasks)}] {task['scene_id']} -> {len(rows)} 条")
            except (urllib.error.URLError, KeyError, ValueError, json.JSONDecodeError) as exc:
                errors += 1
                print(f"[{idx}/{len(tasks)}] {task['scene_id']} 失败: {exc}", file=sys.stderr)
            if args.sleep:
                time.sleep(args.sleep)
    print(f"完成：{total} 条原始证据，{errors} 个场景失败。下一步必须跑 lint_evidence.py。")
    return 0 if total else 2


if __name__ == "__main__":
    raise SystemExit(main())
