#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""审计 MinerU content_list，帮助新厂商手册自适应抽取规则。

在正式构建前回答：
  - MinerU 实际产生了哪些 block type / 标题层级？
  - 表头长什么样，被判为 bitfield/summary/enum/other 的比例？
  - 当前寄存器命名 regex 能识别多少锚点？
  - 应给 extraction profile 增加哪些表头别名？

用法：
  python extract/audit_source.py --input mineru_out
  python extract/audit_source.py --input mineru_out --profile my-profile.json --json report.json
"""

from __future__ import annotations

import argparse
import json
import sys
from collections import Counter
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent))
import mineru_to_kb as conv  # noqa: E402


def audit(inputs: list[Path], profile: Path | None = None) -> dict[str, Any]:
    conv._apply_profile(profile)
    files = conv._iter_content_lists(inputs)
    module_counts = Counter(conv._module_from_path(f) for f in files)
    docs: list[dict[str, Any]] = []
    total_blocks: Counter[str] = Counter()
    total_tables: Counter[str] = Counter()
    unknown_headers: Counter[tuple[str, ...]] = Counter()

    for path in files:
        blocks = json.loads(path.read_text(encoding="utf-8"))
        block_types: Counter[str] = Counter()
        heading_levels: Counter[str] = Counter()
        table_kinds: Counter[str] = Counter()
        register_anchors: set[str] = set()
        image_missing_path = 0

        for block in blocks:
            kind = str(block.get("type", "<missing>"))
            block_types[kind] += 1
            total_blocks[kind] += 1
            if kind == "text":
                text = conv._clean_text(block.get("text"))
                if block.get("text_level"):
                    heading_levels[str(block["text_level"])] += 1
                token = conv._extract_register_token(text)
                if token:
                    register_anchors.add(token)
            elif kind == "table":
                rows = conv._table_rows(block.get("table_body", ""))
                if rows:
                    classified = conv._classify_table(rows[0])
                    table_kinds[classified] += 1
                    total_tables[classified] += 1
                    if classified == "other":
                        unknown_headers[tuple(rows[0])] += 1
            elif kind in ("image", "chart") and not block.get("img_path"):
                image_missing_path += 1

        docs.append({
            "file": str(path),
            "module": conv._module_from_path(path),
            "blocks": dict(block_types),
            "heading_levels": dict(heading_levels),
            "tables": dict(table_kinds),
            "register_anchor_count": len(register_anchors),
            "register_anchor_examples": sorted(register_anchors)[:12],
            "images_without_path": image_missing_path,
        })

    warnings: list[str] = []
    duplicates = {m: n for m, n in module_counts.items() if n > 1}
    if duplicates:
        warnings.append(
            f"同一模块存在多个 content_list {duplicates}；不要同时传分段产物与合并产物，否则会重复摄入。"
        )
    if files and total_tables["bitfield"] == 0:
        warnings.append("未识别到 bitfield 表；检查表头并在 profile.table_header_aliases 添加 bits/field 别名。")
    if files and total_tables["summary"] == 0:
        warnings.append("未识别到 register summary/address 表；检查 register/address/offset 表头别名。")
    if files and sum(d["register_anchor_count"] for d in docs) == 0:
        warnings.append("寄存器锚点为 0；当前 register_name_pattern 不匹配该厂商命名。")
    if unknown_headers:
        warnings.append(
            f"有 {sum(unknown_headers.values())} 张 other 表；先检查下方高频表头，必要时扩展 profile。"
        )

    return {
        "files": len(files),
        "modules": dict(module_counts),
        "block_types": dict(total_blocks),
        "table_types": dict(total_tables),
        "unknown_table_headers": [
            {"count": count, "header": list(header)}
            for header, count in unknown_headers.most_common(20)
        ],
        "warnings": warnings,
        "documents": docs,
    }


def _print_report(report: dict[str, Any]) -> None:
    print(f"文件={report['files']} 模块={report['modules']}")
    print(f"Block types: {report['block_types']}")
    print(f"Table types: {report['table_types']}")
    for doc in report["documents"]:
        print(
            f"- {doc['module']}: blocks={doc['blocks']} headings={doc['heading_levels']} "
            f"tables={doc['tables']} register_anchors={doc['register_anchor_count']}"
        )
        if doc["register_anchor_examples"]:
            print("  anchors:", ", ".join(doc["register_anchor_examples"]))
    if report["unknown_table_headers"]:
        print("\n高频未分类表头：")
        for item in report["unknown_table_headers"][:10]:
            print(f"  x{item['count']}: {' | '.join(item['header'])}")
    if report["warnings"]:
        print("\n警告/适配建议：")
        for warning in report["warnings"]:
            print(f"  ! {warning}")


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description="审计 MinerU content_list，发现新厂商适配点。")
    p.add_argument("--input", nargs="+", required=True)
    p.add_argument("--profile", help="可选 extraction profile JSON")
    p.add_argument("--json", help="可选：同时写出机器可读报告")
    args = p.parse_args(argv)
    try:
        report = audit(
            [Path(x) for x in args.input],
            Path(args.profile) if args.profile else None,
        )
    except (OSError, json.JSONDecodeError, ValueError) as exc:
        print(f"审计失败：{exc}", file=sys.stderr)
        return 2
    _print_report(report)
    if args.json:
        Path(args.json).write_text(
            json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8"
        )
    return 0 if report["files"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
