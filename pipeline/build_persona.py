#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-only
"""模型 + 模板 -> 人格卡（最终系统提示词）。

单一事实源是 persona/kurisu_persona_model.json；本脚本只做渲染与自检，
不改写任何人格表述。`--check` 用于 CI / 提交前校验。

用法:
    python pipeline/build_persona.py                 # 渲染 persona/kurisu.md
    python pipeline/build_persona.py --check         # 只校验，不写文件
    python pipeline/build_persona.py --state state/current.json   # 注入真实状态块
"""
from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
MODEL = ROOT / "persona" / "kurisu_persona_model.json"
TEMPLATE = ROOT / "persona" / "card_template.md"
OUT = ROOT / "persona" / "kurisu.md"
CASES = ROOT / "eval" / "cases.jsonl"
MAX_CARD_BYTES = 64 * 1024  # 与 Bot 的人格卡读取上限一致

ID_RE = re.compile(r"^(T|C|V|E|S|L|U)\d{2}$")
STATUS_LABEL = {
    "prior": "先验",
    "weak": "弱证据",
    "supported": "有证据",
    "contradicted": "存在反证",
}


def cell(text: str) -> str:
    """Markdown 表格单元转义。"""
    return str(text).replace("|", "\\|").replace("\n", " ").strip()


def table(headers: list[str], rows: list[list[str]]) -> str:
    head = "| " + " | ".join(headers) + " |"
    sep = "|" + "|".join(["---"] * len(headers)) + "|"
    body = ["| " + " | ".join(cell(c) for c in row) + " |" for row in rows]
    return "\n".join([head, sep, *body])


def tag(item: dict) -> str:
    """显示支持条目数，括号里区分独立场景与限制/反证（v4.4.1 口径）。"""
    label = STATUS_LABEL[item["status"]]
    stats = item.get("evidence_stats") or {}
    if stats:
        supports = stats.get("supports", 0)
        limits = stats.get("limits", 0)
        contradicts = stats.get("contradicts", 0)
        scenes = stats.get("scenes", 0)
        scenes_core = stats.get("scenes_core", 0)
        if supports == 0:
            return label
        extra = f"场景{scenes_core}/{scenes}"
        if limits or contradicts:
            extra += f"｜限{limits}/反{contradicts}"
        return f"{label}×{supports}（{extra}）"
    n = len(item.get("evidence_ids") or [])
    return label if n == 0 else f"{label}×{n}"


def load_json(path: Path) -> dict:
    with path.open("rb") as fh:
        raw = fh.read()
    if raw.startswith(b"\xef\xbb\xbf"):
        raw = raw[3:]
    return json.loads(raw.decode("utf-8"))


def model_hash(model: dict) -> str:
    blob = json.dumps(model, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
    return hashlib.sha256(blob).hexdigest()


def card_version(model: dict, template_text: str) -> str:
    """版本 = 模型 + 模板，模板改动也会换版本（否则同一版本号对应两种卡片）。"""
    blob = (model_hash(model) + template_text).encode()
    return hashlib.sha256(blob).hexdigest()[:12]


def default_state_block() -> str:
    return (
        "- 关系阶段：陌生人（尚未建立任何信任）\n"
        "- 信任度：10 / 100\n"
        "- 熟悉度：0（你不知道对方的职业、专业背景、和你的任何交集）\n"
        "- 当前情绪：平静，略带观察性戒备\n"
        "- 未解决的事：无\n"
        "- 已授权：无（等待用户分别授权）\n"
        "- 未授权（保持关闭）：长期记忆、敏感内容、主动联系、作息学习\n"
        "- 关于对方你确定知道的：这一轮对方实际说过的内容，以及状态/记忆块里明确提供的互动记录\n"
        "- 表达约束：不主动示好；不预设亲密；回答控制在 1–3 句；主动提问不超过一个"
    )


def coverage_note(model: dict) -> str:
    cov = model["coverage"]
    status = cov.get("by_status", {})
    if status.get("supported", 0) == 0:
        return (
            "本卡所有机制均为**先验**（未经原著文本核对），"
            f"已收录证据 {cov.get('evidence_total', 0)} 条。"
            "与三部曲原文冲突时以原文为准。"
        )
    return (
        f"证据 {cov.get('evidence_total', 0)} 条；"
        f"有证据 {status.get('supported', 0)} / 弱 {status.get('weak', 0)} / "
        f"先验 {status.get('prior', 0)}；边界条款 {len(model.get('known_limits', []))} 条（见 §十二）。"
    )


def render(model: dict, state_block: str) -> str:
    m = model
    identity = m["identity"]

    traits = table(
        ["编号", "内核", "状态"],
        [[t["id"], t["statement"], tag(t)] for t in m["core_traits"]],
    )
    cog = table(
        ["编号", "规则", "状态"],
        [[c["id"], c["statement"], tag(c)] for c in m["cognitive_rules"]],
    )

    values = m["values"]
    values_lines = ["优先级：" + " > ".join(values["priority"]), ""]
    values_lines.append(
        table(
            ["编号", "具体含义", "状态"],
            [[v["id"], v["statement"], tag(v)] for v in values["items"]],
        )
    )
    values_lines.append("")
    values_lines.append("**价值冲突时：**")
    values_lines.append("")
    for c in values["conflicts"]:
        values_lines.append(f"- {' 与 '.join(c['between'])}：{c['resolution']}")
    values_block = "\n".join(values_lines)

    private_items = (
        m.get("private_interest", []) + m.get("privacy_boundary", []) + m.get("self_image", [])
    )
    private_table = table(
        ["编号", "内容", "状态"],
        [[i["id"], i["statement"], tag(i)] for i in private_items],
    ) if private_items else "（未建立）"

    SHORT = {
        "E01": "被降格 / 被当小孩", "E02": "能力被认可", "E03": "被证据指出错误",
        "E04": "被关心", "E05": "父亲 / 至亲否定", "E06": "被当工具、被同情",
        "E07": "无法解释的现象", "E08": "被欺骗", "E09": "过劳 / 被要求休息",
    }
    NEG = ("没有改变", "未改变", "没有事后反省", "没有夺走", "没有被情绪", "并没有改变", "未受影响")

    def effect_cell(e: dict, key: str) -> str:
        ef = (e.get("effects") or {}).get(key) or {}
        text = ef.get("statement", "")
        if not text:
            return ""
        for prefix in ("恢复：", "反省：", "处理：", "注意：", "行动：", "目标："):
            if text.startswith(prefix):
                text = text[len(prefix):]
        mark = ""
        if any(n in text for n in NEG):
            mark = "（反例：此处并未改变）"
        else:
            mark = {"supported": "", "weak": "（弱）", "none": "（未证实）"}.get(ef.get("status"), "")
        return text + mark

    has_effects = any(
        (v or {}).get("statement")
        for e in m["emotion_triggers"]
        for v in (e.get("effects") or {}).values()
    )
    effect_lines = []
    for e in m["emotion_triggers"]:
        cells = {
            "信息处理": effect_cell(e, "processing"),
            "注意力": effect_cell(e, "attention"),
            "行动意愿": effect_cell(e, "action"),
            "对话目标": effect_cell(e, "goal"),
            "恢复 / 反省": "；".join(
                x for x in (
                    f"恢复——{effect_cell(e, 'recovery')}" if effect_cell(e, "recovery") else "",
                    f"反省——{effect_cell(e, 'reflection')}" if effect_cell(e, "reflection") else "",
                ) if x
            ),
        }
        if not any(cells.values()):
            continue
        short = SHORT.get(e["id"]) or e["trigger"].split("，")[0][:16]
        effect_lines.append(f"**{e['id']} · {short}**")
        for label, text in cells.items():
            if text:
                effect_lines.append(f"- {label}：{text}")
        effect_lines.append("")
    effects_block = "\n".join(effect_lines) if has_effects else "（本版尚未补充情绪效应的逐项证据；先按上面的通用规则执行，不要自行发明。）"

    emo = table(
        ["触发", "她怎么理解", "表现", "强度/持续", "怎么恢复"],
        [
            [
                e["trigger"],
                e["appraisal"],
                e["response"],
                f"{e.get('intensity', '—')} / {e.get('duration', '—')}",
                e.get("recovery", "—"),
            ]
            for e in m["emotion_triggers"]
        ],
    )

    social = m["social_model"]
    social_lines = [
        table(["阶段", "表现"], [[s["id"], s["statement"]] for s in social["stages"]]),
        "",
        "**信任怎么建立：**",
        "",
        *[f"- {x}" for x in social["trust_builders"]],
        "",
        "**什么会毁掉信任：**",
        "",
        *[f"- {x}" for x in social["trust_breakers"]],
        "",
        f"**冲突与道歉：** {social['conflict_repair']['statement']}",
    ]
    social_block = "\n".join(social_lines)

    lang = m["language_model"]
    lang_lines = [
        f"**语域：** {lang['register']}",
        "",
        table(["编号", "特征", "状态"], [[f["id"], f["statement"], tag(f)] for f in lang["features"]]),
        "",
        "**禁用：**",
        "",
        *[f"- {b}" for b in lang["banned"]],
    ]
    language_block = "\n".join(lang_lines)

    anti = "\n".join(f"- {a}" for a in m["anti_patterns"])
    anchor = m["knowledge_boundary"]["anchor"]

    status = m["coverage"].get("by_status", {})
    total_items = (
        len(m["core_traits"]) + len(m["cognitive_rules"]) + len(m["values"]["items"])
        + len(m["emotion_triggers"]) + len(m["social_model"]["stages"]) + 1
        + len(m["language_model"]["features"])
        + len(m.get("private_interest", [])) + len(m.get("privacy_boundary", []))
        + len(m.get("self_image", []))
    )
    model_status = (
        "先验草稿（未核对原文）"
        if status.get("supported", 0) == 0
        else f"证据版（{status.get('supported', 0)}/{total_items} 条机制有支持，证据 {m['coverage'].get('evidence_total', 0)} 条）"
    )

    limits_block = "\n".join(
        f"- **{'/'.join(x['mechanisms'])} 边界：** {x['statement']}" for x in m.get("known_limits", [])
    ) or "- （暂无已核实的边界证据）"

    guide = m.get("runtime_guidance") or {}
    guide_lines = []
    for section in guide.get("sections", []):
        guide_lines.extend([f"**{section['title']}**", "", *[f"- {rule}" for rule in section["rules"]], ""])
    guide_lines.append("*以上为运行校准假说，不计入原著证据条目或机制支持率。*")
    guidance_block = "\n".join(guide_lines)

    body = load_text(TEMPLATE)
    mapping = {
        "PERSONA_VERSION": card_version(m, load_text(TEMPLATE)),
        "GENERATED_AT": m.get("generated_at", ""),
        "MODEL_STATUS": model_status,
        "KNOWLEDGE_ANCHOR": anchor,
        "TRAITS_TABLE": traits,
        "COGNITIVE_TABLE": cog,
        "VALUES_BLOCK": values_block,
        "PRIVATE_TABLE": private_table,
        "EMOTION_TABLE": emo,
        "EMOTION_EFFECTS_BLOCK": effects_block,
        "RUNTIME_GUIDANCE": guidance_block,
        "SOCIAL_BLOCK": social_block,
        "LANGUAGE_BLOCK": language_block,
        "ANTI_PATTERN_LIST": anti,
        "KNOWN_LIMITS": limits_block,
        "STATE_BLOCK": state_block,
        "COVERAGE_NOTE": coverage_note(m),
    }
    for key, value in mapping.items():
        body = body.replace("{{" + key + "}}", value)
    return body


def load_text(path: Path) -> str:
    raw = path.read_bytes()
    if raw.startswith(b"\xef\xbb\xbf"):
        raw = raw[3:]
    return raw.decode("utf-8").replace("\r\n", "\n").replace("\r", "\n")


def validate(model: dict, card: str) -> list[str]:
    problems: list[str] = []
    for key in ("identity", "runtime_guidance", "core_traits", "cognitive_rules", "values", "emotion_triggers",
                "social_model", "language_model", "knowledge_boundary", "anti_patterns",
                "uncertainties", "coverage"):
        if key not in model:
            problems.append(f"模型缺少字段: {key}")
    if problems:
        return problems

    ids: set[str] = set()
    for group in ("core_traits", "cognitive_rules"):
        for item in model[group]:
            if not ID_RE.match(item["id"]):
                problems.append(f"{group} 非法 id: {item['id']}")
            ids.add(item["id"])
    for group in ("items",):
        for item in model["values"][group]:
            if not ID_RE.match(item["id"]):
                problems.append(f"values 非法 id: {item['id']}")
            ids.add(item["id"])
    for item in model["emotion_triggers"]:
        if not item["id"].startswith("E"):
            problems.append(f"emotion 非法 id: {item['id']}")
        ids.add(item["id"])
    for item in model["social_model"]["stages"]:
        ids.add(item["id"])
    ids.add(model["social_model"]["conflict_repair"]["id"])
    for item in model["language_model"]["features"]:
        ids.add(item["id"])
    for item in model["uncertainties"]:
        ids.add(item["id"])

    # 证据引用必须指向已知条目
    for item in model["core_traits"] + model["cognitive_rules"] + model["values"]["items"]:
        for ev in item.get("evidence_ids") or []:
            if not ev:
                problems.append(f"{item['id']} 空证据引用")

    leftover = re.findall(r"\{\{[A-Z_]+\}\}", card)
    if leftover:
        problems.append(f"模板占位符未渲染: {sorted(set(leftover))}")

    if not card.startswith("# "):
        problems.append("人格卡首行必须是 '# ' 标题（Bot 用它取角色名）")
    if card.count("\n# ") > 0:
        problems.append("人格卡只能有一个一级标题")

    size = len(card.encode("utf-8"))
    if size > MAX_CARD_BYTES:
        problems.append(f"人格卡 {size} 字节，超过 {MAX_CARD_BYTES} 上限")

    if "{{" in card:
        problems.append("残留 '{{'")

    # 评估用例引用的机制必须存在
    if CASES.exists():
        for line in CASES.read_text("utf-8").splitlines():
            line = line.strip()
            if not line:
                continue
            case = json.loads(line)
            for mech in case.get("expected_mechanisms", []):
                if mech not in ids:
                    problems.append(f"用例 {case['case_id']} 引用未知机制 {mech}")

    # 防御性检查：这些短语若作为正面示范出现，说明卡被改坏了
    for bad in ("好的主人", "都听你的", "人家才不是"):
        if bad in card and "不叫" not in card:
            problems.append(f"出现疑似软化表述: {bad}")

    cov = model["coverage"]
    if cov.get("evidence_total", 0) == 0 and not any("先验" in g for g in cov.get("gaps", [])):
        problems.append("coverage.gaps 未如实声明先验状态")
    for u in model["uncertainties"]:
        if not u.get("resolution_evidence"):
            problems.append(f"{u['id']} 缺少补证方式")
    return problems


def main() -> int:
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass
    ap = argparse.ArgumentParser()
    ap.add_argument("--model", type=Path, default=MODEL)
    ap.add_argument("--template", type=Path, default=TEMPLATE)
    ap.add_argument("--out", type=Path, default=OUT)
    ap.add_argument("--state", type=Path, default=None, help="state.py 输出的 JSON；缺省用陌生人默认块")
    ap.add_argument("--check", action="store_true")
    args = ap.parse_args()

    model = load_json(args.model)
    if args.state and args.state.exists():
        st = load_json(args.state)
        state_block = st.get("block") or default_state_block()
    else:
        state_block = default_state_block()

    card = render(model, state_block)
    problems = validate(model, card)
    size = len(card.encode("utf-8"))

    if problems:
        print("人格卡自检失败：", file=sys.stderr)
        for p in problems:
            print(" -", p, file=sys.stderr)
        return 1

    if args.check:
        print(f"OK  {args.out.relative_to(ROOT) if args.out.is_relative_to(ROOT) else args.out}  "
              f"{size} bytes / {card.count(chr(10)) + 1} lines  "
              f"persona_version={card_version(model, load_text(TEMPLATE))}  状态=未写盘")
        return 0

    args.out.write_text(card, encoding="utf-8", newline="\n")
    print(f"已渲染 {args.out}  {size} bytes  persona_version={card_version(model, load_text(TEMPLATE))}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
