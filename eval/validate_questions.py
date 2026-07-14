#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""验证检索评测 questions.jsonl 的 schema，并可选检查 expected 锚点是否存在于 KB。"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

_EXPECTED_KEYS = {
    "register": {"register_name"},
    "figure": {"caption_contains"},
    "prose": {"section_contains"},
}


def load_and_validate(path: Path, kb: dict[str, Any] | None = None) -> tuple[list[dict[str, Any]], list[str]]:
    questions: list[dict[str, Any]] = []
    errors: list[str] = []
    seen: set[str] = set()
    for line_no, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        if not line.strip():
            continue
        try:
            q = json.loads(line)
        except json.JSONDecodeError as exc:
            errors.append(f"line {line_no}: JSON 错误：{exc}")
            continue
        qid = str(q.get("id", ""))
        qtype = q.get("type")
        if not qid:
            errors.append(f"line {line_no}: 缺 id")
        elif qid in seen:
            errors.append(f"line {line_no}: 重复 id {qid}")
        seen.add(qid)
        if not q.get("query"):
            errors.append(f"{qid or line_no}: 缺 query")
        if qtype not in _EXPECTED_KEYS:
            errors.append(f"{qid or line_no}: type 必须是 {sorted(_EXPECTED_KEYS)}")
        else:
            expected = q.get("expected")
            if not isinstance(expected, dict):
                errors.append(f"{qid}: expected 必须是对象")
            else:
                missing = _EXPECTED_KEYS[qtype] - set(expected)
                if missing:
                    errors.append(f"{qid}: expected 缺 {sorted(missing)}")
            if qtype in ("register", "figure") and not q.get("keyword"):
                errors.append(f"{qid}: {qtype} 题需 keyword（模拟 agent 提炼后的工具参数）")
        if not q.get("source"):
            errors.append(f"{qid or line_no}: 缺 source（必须记录人工核对依据）")
        questions.append(q)

    if kb is not None:
        for q in questions:
            exp = q.get("expected", {})
            module = str(q.get("module", "")).lower()
            if q.get("type") == "register":
                needle = str(exp.get("register_name", "")).lower()
                found = any(
                    (not module or module in str(r.get("module", "")).lower())
                    and needle in str(r.get("register_name", "")).lower()
                    for r in kb.get("registers", [])
                )
                if not found:
                    errors.append(f"{q.get('id')}: KB 中找不到 register expected={needle!r}")
            elif q.get("type") == "figure":
                needle = str(exp.get("caption_contains", "")).lower()
                found = any(
                    (not module or module in str(f.get("module", "")).lower())
                    and needle in str(f.get("caption", "")).lower()
                    for f in kb.get("figures", [])
                )
                if not found:
                    errors.append(f"{q.get('id')}: KB 中找不到 figure expected={needle!r}")
            elif q.get("type") == "prose":
                needle = str(exp.get("section_contains", "")).lower()
                found = any(
                    (not module or module in str(s.get("module", "")).lower())
                    and (
                        needle in str(s.get("title", "")).lower()
                        or needle in str(s.get("text", "")).lower()
                    )
                    for s in kb.get("sections", [])
                )
                if not found:
                    errors.append(f"{q.get('id')}: KB 中找不到 prose expected={needle!r}")
    return questions, errors


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description="验证 eval questions.jsonl。")
    p.add_argument("--questions", required=True)
    p.add_argument("--kb", help="可选：验证 expected 锚点确实存在于 knowledge.json")
    args = p.parse_args(argv)
    try:
        kb = json.loads(Path(args.kb).read_text(encoding="utf-8")) if args.kb else None
        questions, errors = load_and_validate(Path(args.questions), kb)
    except (OSError, json.JSONDecodeError) as exc:
        print(f"读取失败：{exc}", file=sys.stderr)
        return 2
    print(f"questions={len(questions)}")
    for error in errors:
        print(f"ERROR: {error}")
    return 1 if errors else 0


if __name__ == "__main__":
    raise SystemExit(main())
