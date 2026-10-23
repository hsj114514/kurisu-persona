#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-only
"""用仓库自带的 JSON Schema 校验构建产物（不需要第三方库）。

校验对象：
- persona/kurisu_persona_model.json      → schema/persona_model.schema.json
- data/evidence_database.jsonl（每行）    → schema/evidence.schema.json
- eval/eval_cases.jsonl（每行）           → schema/eval_case.schema.json
- eval/cases.jsonl（每行）                → schema/eval_case.schema.json

只实现本项目 schema 用到的关键字：type / required / properties /
additionalProperties / enum / const / pattern / minLength / maxLength /
minimum / maximum / items / minItems / maxItems / oneOf / $ref / $defs。
遇到不支持的关键字会明确报错，而不是静默放过。

用法:
    python pipeline/validate_schemas.py
    python pipeline/validate_schemas.py --max-errors 50
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SCHEMA = ROOT / "schema"
SUPPORTED = {
    "$schema", "$id", "title", "description", "type", "required", "properties",
    "additionalProperties", "enum", "const", "pattern", "minLength", "maxLength",
    "minimum", "maximum", "items", "minItems", "maxItems", "oneOf", "$ref", "$defs",
    "definitions", "default", "examples", "$comment", "format", "uniqueItems",
}


class SchemaError(RuntimeError):
    pass


def load(path: Path):
    return json.loads(path.read_text("utf-8"))


class Validator:
    def __init__(self, root: dict):
        self.root = root

    # ---- helpers ----
    def _resolve(self, schema: dict) -> dict:
        seen = 0
        while isinstance(schema, dict) and "$ref" in schema:
            ref = schema["$ref"]
            if not ref.startswith("#/"):
                raise SchemaError(f"只支持本地 $ref: {ref}")
            node = self.root
            for part in ref[2:].split("/"):
                node = node[part]
            schema = node
            seen += 1
            if seen > 20:
                raise SchemaError(f"$ref 循环: {ref}")
        return schema

    def _type_ok(self, inst, t: str) -> bool:
        if t == "object":
            return isinstance(inst, dict)
        if t == "array":
            return isinstance(inst, list)
        if t == "string":
            return isinstance(inst, str)
        if t == "integer":
            return isinstance(inst, int) and not isinstance(inst, bool)
        if t == "number":
            return isinstance(inst, (int, float)) and not isinstance(inst, bool)
        if t == "boolean":
            return isinstance(inst, bool)
        if t == "null":
            return inst is None
        raise SchemaError(f"不支持的 type: {t}")

    # ---- main ----
    def errors(self, inst, schema: dict, path: str = "$") -> list[str]:
        schema = self._resolve(schema)
        if not isinstance(schema, dict):
            raise SchemaError(f"{path}: schema 不是对象")

        unknown = set(schema) - SUPPORTED
        if unknown:
            raise SchemaError(f"{path}: schema 使用了未实现的关键字 {sorted(unknown)}")

        out: list[str] = []

        if "oneOf" in schema:
            matched = sum(1 for s in schema["oneOf"] if not self.errors(inst, s, path))
            if matched != 1:
                out.append(f"{path}: oneOf 命中 {matched} 个分支（应恰好 1 个）")
            return out

        if "type" in schema:
            types = schema["type"] if isinstance(schema["type"], list) else [schema["type"]]
            if not any(self._type_ok(inst, t) for t in types):
                out.append(f"{path}: 类型应为 {'/'.join(types)}，实际 {type(inst).__name__}")
                return out

        if "enum" in schema and inst not in schema["enum"]:
            out.append(f"{path}: 值 {inst!r} 不在枚举 {schema['enum']}")
        if "const" in schema and inst != schema["const"]:
            out.append(f"{path}: 值应为常量 {schema['const']!r}")

        if isinstance(inst, str):
            if "minLength" in schema and len(inst) < schema["minLength"]:
                out.append(f"{path}: 长度 {len(inst)} < minLength {schema['minLength']}")
            if "maxLength" in schema and len(inst) > schema["maxLength"]:
                out.append(f"{path}: 长度 {len(inst)} > maxLength {schema['maxLength']}")
            if "pattern" in schema and not re.search(schema["pattern"], inst):
                out.append(f"{path}: 不匹配 pattern {schema['pattern']!r}")

        if isinstance(inst, (int, float)) and not isinstance(inst, bool):
            if "minimum" in schema and inst < schema["minimum"]:
                out.append(f"{path}: {inst} < minimum {schema['minimum']}")
            if "maximum" in schema and inst > schema["maximum"]:
                out.append(f"{path}: {inst} > maximum {schema['maximum']}")

        if isinstance(inst, dict):
            for key in schema.get("required", []):
                if key not in inst:
                    out.append(f"{path}: 缺少必需字段 {key}")
            props = schema.get("properties") or {}
            extra = schema.get("additionalProperties", True)
            for key, value in inst.items():
                if key in props:
                    out.extend(self.errors(value, props[key], f"{path}.{key}"))
                elif extra is False:
                    out.append(f"{path}: 不允许的字段 {key}")
                elif isinstance(extra, dict):
                    out.extend(self.errors(value, extra, f"{path}.{key}"))

        if isinstance(inst, list):
            if "minItems" in schema and len(inst) < schema["minItems"]:
                out.append(f"{path}: 元素 {len(inst)} < minItems {schema['minItems']}")
            if "maxItems" in schema and len(inst) > schema["maxItems"]:
                out.append(f"{path}: 元素 {len(inst)} > maxItems {schema['maxItems']}")
            if schema.get("uniqueItems"):
                seen, dup = [], False
                for item in inst:
                    key = json.dumps(item, ensure_ascii=False, sort_keys=True)
                    if key in seen:
                        dup = True
                    seen.append(key)
                if dup:
                    out.append(f"{path}: uniqueItems 要求元素不重复")
            if "items" in schema:
                for i, item in enumerate(inst):
                    out.extend(self.errors(item, schema["items"], f"{path}[{i}]"))

        return out


def read_jsonl(path: Path) -> list[dict]:
    return [json.loads(line) for line in path.read_text("utf-8").splitlines() if line.strip()]


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--max-errors", type=int, default=30)
    args = ap.parse_args()
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

    targets = [
        (SCHEMA / "persona_model.schema.json", ROOT / "persona" / "kurisu_persona_model.json", False),
        (SCHEMA / "evidence.schema.json", ROOT / "data" / "evidence_database.jsonl", True),
        (SCHEMA / "eval_case.schema.json", ROOT / "eval" / "eval_cases.jsonl", True),
        (SCHEMA / "eval_case.schema.json", ROOT / "eval" / "cases.jsonl", True),
    ]

    all_errors: list[str] = []
    checked = 0
    for schema_path, data_path, is_jsonl in targets:
        if not data_path.exists() and is_jsonl:
            print(f"SKIP {data_path.relative_to(ROOT)} (local data not distributed)")
            continue
        checked += 1
        schema = load(schema_path)
        v = Validator(schema)
        if is_jsonl:
            rows = read_jsonl(data_path)
            errors = []
            for i, row in enumerate(rows):
                errors.extend(v.errors(row, schema, f"{data_path.name}[{i}]"))
            label = f"{data_path.relative_to(ROOT)}（{len(rows)} 行）"
        else:
            inst = load(data_path)
            errors = v.errors(inst, schema, data_path.name)
            label = str(data_path.relative_to(ROOT))
        if errors:
            all_errors.extend(errors)
            print(f"FAIL {label}  schema={schema_path.name}  {len(errors)} 处不通过")
        else:
            print(f"OK   {label}  schema={schema_path.name}")

    if all_errors:
        print(f"\n共 {len(all_errors)} 处不通过，前 {min(args.max_errors, len(all_errors))} 条：")
        for err in all_errors[: args.max_errors]:
            print(" -", err)
        return 1
    print(f"\n结论: 通过（{checked} 个已提供目标符合 schema；未分发的本地数据未验证）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
