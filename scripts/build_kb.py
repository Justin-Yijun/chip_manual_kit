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

环境变量：
    CHIP_EMBED_MODEL   默认嵌入模型（--embed-model 覆盖）
    MINERU_MODEL_SOURCE=modelscope   受限网络下载模型源（仅解析 PDF 阶段用）
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "extract"))


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

    if args.skip_vectors:
        print("已跳过向量索引（--skip-vectors）。混合检索将只有 BM25。")
        return 0

    import vectorize
    print("== [2/2] knowledge.json → data/vectors/（分块 + dense 向量）==")
    rc = vectorize.main(["--kb", str(kb_json), "--out", str(vectors_dir), "--model", args.embed_model])
    if rc != 0:
        print(f"vectorize 失败（rc={rc}）。knowledge.json 已生成，混合检索可先用 BM25。", file=sys.stderr)
        return rc

    print(f"完成：{kb_json} + {vectors_dir}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
