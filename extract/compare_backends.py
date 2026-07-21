#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""对比两条抽取后端产出的 knowledge.json（或 content_list 目录）。

典型用法：同一 golden-slice PDF 分别经 MinerU / Docling → knowledge.json，再：

    python compare_backends.py --a data/kb_mineru.json --b data/kb_docling.json

或先比中间层：

    python compare_backends.py --a-content out_mineru --b-content out_docling

输出：寄存器集合交并、地址一致率、位域名一致率、章节/图/枚举计数差，
以及建议下一步（profile / 人工抽查）。不改写任何输入文件。
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent))
import mineru_to_kb as conv  # noqa: E402


def _load_kb(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def _kb_from_content_dirs(inputs: list[Path], profile: Path | None) -> dict[str, Any]:
    conv._apply_profile(profile)
    files = conv._iter_content_lists(inputs)
    kb: dict[str, Any] = {
        "modules": [],
        "registers": [],
        "sections": [],
        "enums": [],
        "formulas": [],
        "algorithms": [],
        "figures": [],
        "tables": [],
    }
    for f in files:
        module = conv._module_from_path(f)
        part = conv._convert_doc(json.loads(f.read_text(encoding="utf-8")), module)
        if module not in kb["modules"]:
            kb["modules"].append(module)
        for key in ("registers", "sections", "enums", "formulas", "algorithms", "figures", "tables"):
            kb[key].extend(part[key])
    return kb


def _reg_index(kb: dict[str, Any]) -> dict[str, dict[str, Any]]:
    out: dict[str, dict[str, Any]] = {}
    for reg in kb.get("registers", []):
        name = (reg.get("register_name") or "").upper()
        if name:
            out[name] = reg
    return out


def _field_names(reg: dict[str, Any]) -> set[str]:
    return {
        (f.get("name") or "").upper()
        for f in reg.get("bit_fields", [])
        if f.get("name")
    }


def compare_kbs(a: dict[str, Any], b: dict[str, Any], label_a: str, label_b: str) -> dict[str, Any]:
    ra, rb = _reg_index(a), _reg_index(b)
    set_a, set_b = set(ra), set(rb)
    both = set_a & set_b
    only_a = sorted(set_a - set_b)
    only_b = sorted(set_b - set_a)

    addr_agree = 0
    addr_disagree: list[dict[str, str]] = []
    field_jaccard: list[float] = []
    for name in sorted(both):
        aa, bb = (ra[name].get("address") or "").lower(), (rb[name].get("address") or "").lower()
        if aa and bb and aa == bb:
            addr_agree += 1
        elif aa or bb:
            addr_disagree.append({"register": name, label_a: aa, label_b: bb})
        fa, fb = _field_names(ra[name]), _field_names(rb[name])
        if fa or fb:
            field_jaccard.append(len(fa & fb) / len(fa | fb))

    def counts(kb: dict[str, Any]) -> dict[str, int]:
        return {
            "modules": len(kb.get("modules", [])),
            "registers": len(kb.get("registers", [])),
            "sections": len(kb.get("sections", [])),
            "enums": len(kb.get("enums", [])),
            "formulas": len(kb.get("formulas", [])),
            "algorithms": len(kb.get("algorithms", [])),
            "figures": len(kb.get("figures", [])),
            "tables": len(kb.get("tables", [])),
        }

    report: dict[str, Any] = {
        "counts": {label_a: counts(a), label_b: counts(b)},
        "registers": {
            "intersection": len(both),
            "only_" + label_a: only_a[:40],
            "only_" + label_b: only_b[:40],
            "only_" + label_a + "_count": len(only_a),
            "only_" + label_b + "_count": len(only_b),
            "jaccard": (len(both) / len(set_a | set_b)) if (set_a or set_b) else 1.0,
        },
        "addresses": {
            "compared_in_intersection": len(both),
            "agree": addr_agree,
            "disagree_examples": addr_disagree[:20],
        },
        "bit_fields": {
            "mean_jaccard_on_shared_registers": (
                sum(field_jaccard) / len(field_jaccard) if field_jaccard else None
            ),
        },
        "hints": [],
    }

    j = report["registers"]["jaccard"]
    if j < 0.7:
        report["hints"].append(
            "寄存器集合重叠偏低：先对两侧跑 audit_source / validate_kb，检查表头别名与寄存器 regex。"
        )
    if addr_disagree:
        report["hints"].append(
            "共享寄存器地址不一致：人工抽查汇总表 OCR/列映射；确认 summary 表被正确分类。"
        )
    mean_f = report["bit_fields"]["mean_jaccard_on_shared_registers"]
    if mean_f is not None and mean_f < 0.8:
        report["hints"].append(
            "共享寄存器位域名重叠偏低：检查位域位置 regex（profile.bit_position_patterns）与表头别名。"
        )
    if not report["hints"]:
        report["hints"].append("结构重叠良好；仍应用同一 gold 题集跑 eval/run_eval.py 再定默认后端。")
    return report


def _print_report(report: dict[str, Any]) -> None:
    print("=== counts ===")
    for side, c in report["counts"].items():
        print(f"  [{side}] {c}")
    reg = report["registers"]
    print("\n=== registers ===")
    print(f"  intersection={reg['intersection']}  jaccard={reg['jaccard']:.2%}")
    for key in reg:
        if key.startswith("only_") and key.endswith("_count"):
            print(f"  {key}={reg[key]}")
    print("\n=== addresses (shared regs) ===")
    addr = report["addresses"]
    print(f"  agree={addr['agree']} / {addr['compared_in_intersection']}")
    for ex in addr["disagree_examples"][:5]:
        print(f"  disagree: {ex}")
    print("\n=== bit_fields ===")
    mean = report["bit_fields"]["mean_jaccard_on_shared_registers"]
    print(f"  mean_jaccard={mean if mean is None else f'{mean:.2%}'}")
    print("\n=== hints ===")
    for h in report["hints"]:
        print(f"  - {h}")


def main(argv: list[str] | None = None) -> int:
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8")  # type: ignore[attr-defined]
        except (AttributeError, ValueError):
            pass

    p = argparse.ArgumentParser(description="对比 MinerU / Docling 等后端的 knowledge 产出")
    p.add_argument("--a", help="知识库 A（knowledge.json）")
    p.add_argument("--b", help="知识库 B（knowledge.json）")
    p.add_argument("--a-content", nargs="+", help="A 侧 *_content_list.json 目录/文件")
    p.add_argument("--b-content", nargs="+", help="B 侧 *_content_list.json 目录/文件")
    p.add_argument("--label-a", default="a")
    p.add_argument("--label-b", default="b")
    p.add_argument("--profile", help="两侧 content_list→KB 时共用的 profile")
    p.add_argument("--json", dest="json_out", help="把完整报告写到 JSON 文件")
    args = p.parse_args(argv)

    profile = Path(args.profile) if args.profile else None
    if args.a and args.b:
        ka, kb = _load_kb(Path(args.a)), _load_kb(Path(args.b))
    elif args.a_content and args.b_content:
        ka = _kb_from_content_dirs([Path(x) for x in args.a_content], profile)
        kb = _kb_from_content_dirs([Path(x) for x in args.b_content], profile)
    else:
        print("请提供 --a/--b 或 --a-content/--b-content", file=sys.stderr)
        return 2

    report = compare_kbs(ka, kb, args.label_a, args.label_b)
    _print_report(report)
    if args.json_out:
        Path(args.json_out).write_text(
            json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        print(f"\n已写报告 {args.json_out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
