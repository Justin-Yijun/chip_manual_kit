#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""验证 knowledge.json 的结构完整性与抽取覆盖率。

默认：结构错误返回非零；覆盖率异常只告警。加 --strict 后告警也返回非零，适合 CI。
"""

from __future__ import annotations

import argparse
import json
import sys
from collections import Counter
from pathlib import Path
from typing import Any

_COLLECTIONS = (
    "registers", "sections", "enums", "formulas", "algorithms", "figures", "tables", "documents",
)


def validate(kb: dict[str, Any], data_dir: Path) -> dict[str, Any]:
    errors: list[str] = []
    warnings: list[str] = []
    for key in _COLLECTIONS:
        if not isinstance(kb.get(key), list):
            errors.append(f"{key} 必须是数组")

    if errors:
        return {"errors": errors, "warnings": warnings, "metrics": {}}

    modules = [str(x) for x in kb.get("modules", [])]
    section_ids = [s.get("section_id", "") for s in kb["sections"] if s.get("section_id")]
    section_set = set(section_ids)
    dup_sections = [x for x, n in Counter(section_ids).items() if n > 1]
    if dup_sections:
        errors.append(f"重复 section_id（前10）: {dup_sections[:10]}")

    reg_keys = [
        (r.get("module", ""), r.get("register_name", ""))
        for r in kb["registers"] if r.get("register_name")
    ]
    dup_regs = [x for x, n in Counter(reg_keys).items() if n > 1]
    if dup_regs:
        warnings.append(f"重复寄存器（前10）: {dup_regs[:10]}")

    for collection in ("registers", "enums", "formulas", "algorithms", "figures", "tables"):
        orphan = [
            item.get("section_id")
            for item in kb[collection]
            if item.get("section_id") and item.get("section_id") not in section_set
        ]
        if orphan:
            errors.append(f"{collection} 有 {len(orphan)} 个孤立 section_id（例：{orphan[:3]}）")

    missing_images = []
    for fig in kb["figures"]:
        rel = fig.get("img_path", "")
        if rel and not (data_dir / rel).exists():
            missing_images.append(rel)
    if missing_images:
        warnings.append(f"{len(missing_images)} 张图路径不存在（例：{missing_images[:3]}）")

    empty_bitfields = sum(1 for r in kb["registers"] if not r.get("bit_fields"))
    addressed = sum(1 for r in kb["registers"] if r.get("address"))
    figure_context = sum(1 for f in kb["figures"] if f.get("context"))
    section_text = sum(1 for s in kb["sections"] if s.get("text"))
    metrics = {
        "modules": modules,
        **{key: len(kb[key]) for key in _COLLECTIONS},
        "registers_with_address": addressed,
        "registers_with_bit_fields": len(kb["registers"]) - empty_bitfields,
        "sections_with_text": section_text,
        "figures_with_context": figure_context,
    }

    if not modules:
        errors.append("modules 为空")
    if not kb["sections"]:
        warnings.append("sections=0：MinerU 标题层级可能未识别")
    if not kb["registers"]:
        warnings.append("registers=0：检查寄存器 regex、表头别名和寄存器锚点")
    elif empty_bitfields:
        warnings.append(f"{empty_bitfields}/{len(kb['registers'])} 个寄存器没有位域")
    if kb["registers"] and addressed / len(kb["registers"]) < 0.5:
        warnings.append(
            f"仅 {addressed}/{len(kb['registers'])} 个寄存器有地址；检查 summary/address 表头适配"
        )
    if kb["figures"] and figure_context / len(kb["figures"]) < 0.8:
        warnings.append(
            f"仅 {figure_context}/{len(kb['figures'])} 张图有关联正文；检查标题/章节边界"
        )
    return {"errors": errors, "warnings": warnings, "metrics": metrics}


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description="验证 knowledge.json 结构和抽取覆盖率。")
    p.add_argument("--kb", required=True)
    p.add_argument("--strict", action="store_true", help="告警也视为失败（CI 推荐）")
    p.add_argument("--json", help="可选：写机器可读报告")
    args = p.parse_args(argv)
    path = Path(args.kb)
    try:
        kb = json.loads(path.read_text(encoding="utf-8"))
        report = validate(kb, path.parent)
    except (OSError, json.JSONDecodeError) as exc:
        print(f"读取失败：{exc}", file=sys.stderr)
        return 2

    print("Metrics:", json.dumps(report["metrics"], ensure_ascii=False))
    for item in report["errors"]:
        print(f"ERROR: {item}")
    for item in report["warnings"]:
        print(f"WARN: {item}")
    if args.json:
        Path(args.json).write_text(
            json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8"
        )
    if report["errors"] or (args.strict and report["warnings"]):
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
