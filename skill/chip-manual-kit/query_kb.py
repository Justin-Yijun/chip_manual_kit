#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""芯片手册知识库 CLI 查询（零第三方依赖，纯标准库）。

当运行环境禁用 MCP（如受策略限制的 VS Code Copilot）时，AI agent 可直接在终端
调用本脚本查询 knowledge.json，从而在 vibe coding 中获得寄存器地址/位域，以及
原理、公式、算法、枚举等上下文。

用法：
    # 查寄存器（默认）
    python query_kb.py --data knowledge.json --query ctrl_status --module ACME
    # 查原理/公式/算法/枚举/图（概念检索）
    python query_kb.py --data knowledge.json --mode concepts --query "sequence id" --module ACME
    python query_kb.py --data knowledge.json --query jobid --format json
"""

import argparse
import json
import sys
from pathlib import Path
from typing import Any

_DEFAULT_LIMIT = 20

# Windows GBK 控制台无法编码 PDF 抽取残留字形，强制 UTF-8 输出避免崩溃
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8")  # type: ignore[attr-defined]
    except (AttributeError, ValueError):
        pass


def _load(data_file: Path) -> dict[str, Any]:
    if not data_file.exists():
        raise FileNotFoundError(f"未找到知识库 {data_file}，请先用 mineru_to_kb.py 生成。")
    try:
        raw = json.loads(data_file.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError) as exc:
        raise ValueError(f"知识库 {data_file} 读取失败：{exc}") from exc
    for key in ("registers", "sections", "enums", "formulas", "algorithms", "figures", "tables", "documents"):
        raw.setdefault(key, [])
    return raw


def _module_ok(item: dict[str, Any], module_lc: str) -> bool:
    return not module_lc or module_lc in item.get("module", "").lower()


def _reg_haystack(reg: dict[str, Any]) -> str:
    parts = [reg.get("register_name", ""), reg.get("description", "")]
    for f in reg.get("bit_fields", []):
        parts += [f.get("name", ""), f.get("description", "")]
    return " ".join(parts).lower()


def _search_registers(kb: dict[str, Any], query: str, module: str, limit: int):
    q, m = query.strip().lower(), module.strip().lower()
    sec_by_id = {s.get("section_id", ""): s.get("text", "") for s in kb["sections"]}
    out = []
    for reg in kb["registers"]:
        if not _module_ok(reg, m) or (q and q not in _reg_haystack(reg)):
            continue
        r = dict(reg)
        r["related_section"] = sec_by_id.get(reg.get("section_id", ""), "")
        out.append(r)
    return out[:limit], len(out)


def _search_concepts(kb: dict[str, Any], query: str, module: str, limit: int):
    q, m = query.strip().lower(), module.strip().lower()

    def hit(t: str) -> bool:
        return not q or q in (t or "").lower()

    secs = [s for s in kb["sections"] if _module_ok(s, m) and (hit(s.get("title", "")) or hit(s.get("text", "")))]
    fmls = [f for f in kb["formulas"] if _module_ok(f, m) and hit(f.get("latex", ""))]
    algs = [a for a in kb["algorithms"] if _module_ok(a, m) and hit(a.get("text", ""))]
    enums = [
        e for e in kb["enums"]
        if _module_ok(e, m) and (hit(e.get("name", "")) or any(
            hit(v.get("mnemonic", "")) or hit(v.get("description", "")) for v in e.get("values", [])))
    ]
    tbls = [
        t for t in kb["tables"]
        if _module_ok(t, m) and (hit(t.get("caption", "")) or hit(
            " ".join(" ".join(r) for r in t.get("rows", [])) + " " + " ".join(t.get("header", []))))
    ]
    figs = [
        g for g in kb["figures"]
        if _module_ok(g, m) and (hit(g.get("caption", "")) or hit(g.get("context", "")))
    ]
    return {"sections": secs[:limit], "formulas": fmls, "algorithms": algs,
            "enums": enums, "tables": tbls[:limit], "figures": figs}


def _fmt_registers(results, total, limit) -> str:
    lines = [f"命中 {total} 个寄存器" + (f"（仅显示前 {limit} 个）" if total > limit else "")]
    for reg in results:
        lines.append("")
        lines.append(f"● {reg['register_name']}  [{reg.get('module', '')}]")
        lines.append(f"  地址: {reg.get('address') or '（未知）'}   来源页: {reg.get('page', '?')}")
        if reg.get("description"):
            lines.append(f"  分组: {reg['description']}")
        for f in reg.get("bit_fields", []):
            extra = " ".join(x for x in (f.get("access", ""), f.get("reset_value", ""), f.get("typical_value", "")) if x)
            lines.append(f"    {f.get('bit_range', ''):<10} {f.get('name', ''):<26} {extra:<14} {f.get('description', '')}")
        if reg.get("related_section"):
            lines.append(f"  解说: {reg['related_section'][:200]}")
    return "\n".join(lines)


def _fmt_concepts(res) -> str:
    lines = [f"命中: sections={len(res['sections'])} formulas={len(res['formulas'])} "
             f"algorithms={len(res['algorithms'])} enums={len(res['enums'])} "
             f"tables={len(res['tables'])} figures={len(res['figures'])}"]
    for s in res["sections"]:
        lines.append(f"\n§ {s.get('title', '')}  [{s.get('module', '')} p{s.get('page', '?')}]")
        lines.append(f"  {s.get('text', '')[:300]}")
    for f in res["formulas"]:
        lines.append(f"\n∑ [{f.get('module', '')}] {f.get('latex', '')}")
    for a in res["algorithms"]:
        cap = a.get("caption") or a.get("sub_type", "")
        lines.append(f"\n» ALGO [{a.get('module', '')}] {cap}")
        lines.append(f"    {a.get('text', '')[:400]}")
    for e in res["enums"]:
        lines.append(f"\n≡ ENUM {e.get('name', '')}  [{e.get('module', '')}]")
        for v in e.get("values", [])[:40]:
            lines.append(f"    {v.get('value', ''):<8} {v.get('mnemonic', ''):<28} {v.get('description', '')[:120]}")
    for t in res["tables"]:
        lines.append(f"\n▦ TABLE [{t.get('module', '')} p{t.get('page', '?')}] {t.get('caption', '')}")
        lines.append("    " + " | ".join(t.get("header", [])))
        for r in t.get("rows", [])[:8]:
            lines.append("    " + " | ".join(r))
    for g in res["figures"]:
        lines.append(f"\n▣ FIG [{g.get('module', '')}] {g.get('caption', '')}  → {g.get('img_path', '')}")
        if g.get("context"):
            lines.append(f"    说明: {g['context'][:200]}")
    return "\n".join(lines)


def _parse_args(argv=None):
    p = argparse.ArgumentParser(description="查询 knowledge.json（无需 MCP）。")
    p.add_argument("--data", default="knowledge.json", help="knowledge.json 路径。")
    p.add_argument("--mode", choices=("registers", "concepts"), default="registers", help="检索模式。")
    p.add_argument("--query", default="", help="关键词。")
    p.add_argument("--module", default="", help="外设模块过滤，如 ACME/FOO。")
    p.add_argument("--limit", type=int, default=_DEFAULT_LIMIT, help="返回条数上限。")
    p.add_argument("--format", choices=("text", "json"), default="text", help="输出格式。")
    return p.parse_args(argv)


def main(argv=None) -> int:
    args = _parse_args(argv)
    try:
        kb = _load(Path(args.data))
    except (FileNotFoundError, ValueError) as exc:
        print(exc, file=sys.stderr)
        return 1

    if args.mode == "registers":
        results, total = _search_registers(kb, args.query, args.module, args.limit)
        if args.format == "json":
            print(json.dumps({"mode": "registers", "count": len(results), "total": total, "results": results},
                             ensure_ascii=False, indent=2))
        else:
            print(_fmt_registers(results, total, args.limit))
    else:
        res = _search_concepts(kb, args.query, args.module, args.limit)
        if args.format == "json":
            print(json.dumps({"mode": "concepts", **res}, ensure_ascii=False, indent=2))
        else:
            print(_fmt_concepts(res))
    return 0


if __name__ == "__main__":
    sys.exit(main())
