#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""一键构建知识库 + 混合检索索引（工具无关，Cursor / VS Code / Claude Code 均可调用）。

把两步串成一个可复现入口：
    1) MinerU 输出（*_content_list.json）→ data/knowledge.json   （extract/mineru_to_kb.py）
    2) knowledge.json → data/vectors/（chunks + dense 向量 + meta）（extract/vectorize.py）

BM25 词法索引与 RRF 融合是纯标准库、在 server 端按 chunks.json 现算，无需在此生成。
cross-encoder 精排为可选，设 CHIP_RERANK_MODEL 即在查询时启用（本脚本不下载模型）。

用法：
    # 从已有 MinerU 输出目录重建（最常见）
    python scripts/build_kb.py --mineru-out ../mineru-work/out_acme ../mineru-work/out_other

    # 指定本地嵌入模型（离线）
    python scripts/build_kb.py --mineru-out OUT... --embed-model /path/to/bge-small-en-v1.5

    # 其它厂商：加载寄存器命名/表头别名 profile
    python scripts/build_kb.py --mineru-out OUT... --profile my-vendor-profile.json

    # 只重建 knowledge.json，跳过向量（无 torch 环境）
    python scripts/build_kb.py --mineru-out OUT... --skip-vectors

    # 主库建完后，用 Docling 对原始 PDF 再抽一遍并打印分歧报告（不覆盖主库）
    python scripts/build_kb.py --mineru-out OUT... --verify-with-docling manual.pdf
    # 模块名与 --mineru-out 推断的不一致时，用 MODULE=path 显式指定；可传多份 PDF
    python scripts/build_kb.py --mineru-out OUT... --verify-with-docling ACME=manual.pdf FOO=foo.pdf

环境变量：
    CHIP_EMBED_MODEL   默认嵌入模型（--embed-model 覆盖）
    MINERU_MODEL_SOURCE=modelscope   受限网络下载模型源（仅解析 PDF 阶段用）
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "extract"))

_KB_COLLECTIONS = ("registers", "sections", "enums", "formulas", "algorithms", "figures", "tables")


def _filter_kb_by_module(kb: dict[str, Any], module: str) -> dict[str, Any]:
    """从整库 knowledge.json 里切出单个模块的切片，供逐模块对照（否则其它模块会
    被 compare_backends 误报成"only_mineru"，因为 Docling 那侧只跑了这一个 PDF）。"""
    module = module.upper()
    out: dict[str, Any] = {"modules": [module]}
    for key in _KB_COLLECTIONS:
        out[key] = [item for item in kb.get(key, []) if str(item.get("module", "")).upper() == module]
    return out


def _parse_docling_targets(entries: list[str]) -> list[tuple[str, Path]]:
    """解析 --verify-with-docling 的条目：'path/to.pdf' 或 'MODULE=path/to.pdf'。"""
    targets: list[tuple[str, Path]] = []
    for entry in entries:
        if "=" in entry:
            module, pdf = entry.split("=", 1)
            module = module.strip().upper()
        else:
            pdf = entry
            module = Path(entry).stem.upper()
        targets.append((module, Path(pdf)))
    return targets


def _run_docling_verification(
    targets: list[tuple[str, Path]],
    main_kb: dict[str, Any],
    data_dir: Path,
    profile: str | None,
) -> bool:
    """对每个 (module, pdf) 跑 Docling 抽取，与主库该模块切片对照并打印/落盘报告。

    不修改 main_kb / 不覆盖主库文件。返回 True 表示所有模块都对照成功；
    某个模块因缺依赖/PDF 读取失败等原因跳过时返回 False（但不影响主库已构建成功）。
    """
    import compare_backends
    import docling_to_kb

    verify_dir = data_dir / "docling_verify"
    verify_dir.mkdir(parents=True, exist_ok=True)
    profile_path = Path(profile) if profile else None
    all_ok = True

    for module, pdf_path in targets:
        print(f"\n== Docling 校验对照：module={module}, pdf={pdf_path} ==")
        if not pdf_path.exists():
            print(f"  跳过：PDF 不存在 {pdf_path}", file=sys.stderr)
            all_ok = False
            continue
        try:
            doc = docling_to_kb.convert_pdf_with_docling(pdf_path)
        except SystemExit as exc:
            print(f"  跳过：{exc}", file=sys.stderr)
            all_ok = False
            continue
        except Exception as exc:  # noqa: BLE001 - Docling 环境相关错误不应中断整个构建
            print(f"  跳过：Docling 解析失败 - {exc}", file=sys.stderr)
            all_ok = False
            continue

        blocks = docling_to_kb.docling_document_to_content_list(doc)
        docling_kb_path = verify_dir / f"kb_docling_{module}.json"
        docling_kb = docling_to_kb.build_knowledge([(module, blocks)], docling_kb_path, profile_path)

        mineru_slice = _filter_kb_by_module(main_kb, module)
        if not mineru_slice["registers"] and not mineru_slice["sections"]:
            print(f"  注意：主库里没有 module={module} 的内容，请检查模块名是否匹配", file=sys.stderr)

        report = compare_backends.compare_kbs(mineru_slice, docling_kb, "mineru", "docling")
        compare_backends._print_report(report)

        report_path = data_dir / f"docling_compare_{module}.json"
        report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"  已写分歧报告：{report_path}")

    print(
        "\n提示：以上分歧仅供人工回原书核对，不会自动合并/覆盖主库；"
        "确认后按 docs/USER_GUIDE.md §3 手动修 profile 或补抽取规则再重建主库。"
    )
    return all_ok


def main(argv: list[str] | None = None) -> int:
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8")  # type: ignore[attr-defined]
        except (AttributeError, ValueError):
            pass

    p = argparse.ArgumentParser(description="一键构建 knowledge.json + 混合检索索引。")
    p.add_argument("--mineru-out", nargs="+", required=True,
                   help="MinerU/Docling 产出的目录或 *_content_list.json（可多个）")
    p.add_argument("--data-dir", default=str(ROOT / "data"),
                   help="输出目录（默认 data/），产出 knowledge.json 与 vectors/")
    p.add_argument("--embed-model", default=os.environ.get("CHIP_EMBED_MODEL", "BAAI/bge-small-en-v1.5"),
                   help="dense 嵌入模型名或本地路径")
    p.add_argument("--profile", help="可选厂商抽取 profile JSON（传给 mineru_to_kb.py）")
    p.add_argument("--skip-vectors", action="store_true", help="只建 knowledge.json，跳过向量索引")
    p.add_argument(
        "--verify-with-docling", nargs="+", metavar="PDF|MODULE=PDF",
        help="主库建完后，用 Docling 重新解析这些原始 PDF 并与主库同模块切片对照，"
             "打印+落盘分歧报告（data/docling_compare_<MODULE>.json）。不装 docling 会提示但不中断主库构建。"
             "模块名默认取 PDF 文件名（不含扩展名），不一致时用 MODULE=path 显式指定。",
    )
    args = p.parse_args(argv)

    data_dir = Path(args.data_dir)
    data_dir.mkdir(parents=True, exist_ok=True)
    kb_json = data_dir / "knowledge.json"
    vectors_dir = data_dir / "vectors"

    import mineru_to_kb
    print("== [1/2] content_list → knowledge.json ==")
    extract_args = ["--input", *args.mineru_out, "--output", str(kb_json)]
    if args.profile:
        extract_args.extend(["--profile", args.profile])
    rc = mineru_to_kb.main(extract_args)
    if rc != 0:
        print(f"mineru_to_kb 失败（rc={rc}）", file=sys.stderr)
        return rc

    verification_incomplete = False
    if args.verify_with_docling:
        print("\n== [可选] Docling 校验对照（不覆盖主库）==")
        targets = _parse_docling_targets(args.verify_with_docling)
        main_kb = json.loads(kb_json.read_text(encoding="utf-8"))
        verification_incomplete = not _run_docling_verification(
            targets, main_kb, data_dir, args.profile
        )

    if args.skip_vectors:
        print("已跳过向量索引（--skip-vectors）。混合检索将只有 BM25。")
        return 3 if verification_incomplete else 0

    import vectorize
    print("== [2/2] knowledge.json → data/vectors/（分块 + dense 向量）==")
    rc = vectorize.main(["--kb", str(kb_json), "--out", str(vectors_dir), "--model", args.embed_model])
    if rc != 0:
        print(f"vectorize 失败（rc={rc}）。knowledge.json 已生成，混合检索可先用 BM25。", file=sys.stderr)
        return rc

    print(f"完成：{kb_json} + {vectors_dir}")
    if verification_incomplete:
        print("注意：部分/全部 Docling 校验未完成（缺依赖或PDF问题），主库已正常构建，详见上方日志。", file=sys.stderr)
        return 3
    return 0


if __name__ == "__main__":
    sys.exit(main())
