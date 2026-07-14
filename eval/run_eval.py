#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""芯片手册知识库检索评测：跑 eval/questions.jsonl，报 hit@1 / hit@5。

设计目的：量化 search_prose 的混合检索算法（BM25 / RRF / rerank）到底对结果影响多大，
而不是拍脑袋判断。同时给出 search_registers / search_concepts 的基线结果——它们是
确定性的结构化/关键词匹配，不受 search_prose 算法变化影响，作为对照。

题型（question["type"]）→ 走哪条路径：
    register  → chip_server.search_registers（结构化，不受混合检索算法影响）
    figure    → chip_server.search_concepts（关键词匹配 caption/context）
    prose     → server.retrieval.HybridIndex，对比多种配置：

关于 query 与 keyword 两个字段：search_registers/search_concepts 是**字面子串匹配**
（不分词、不做语义），传自然语言整句基本不会命中——真实使用中，是 agent（LLM）先把
用户问题提炼成关键词再调用工具，而不是把原句转发进去。因此 register/figure 类题目
额外带 "keyword" 字段，模拟这一步提炼后的调用参数；"query" 仍保留原始自然语言，
供人读、也供 prose 类题目直接使用（prose 走语义/词法混合检索，能吃自然语言）。
                  bm25         : 只用 BM25（use_dense=False, rerank=False）
                  hybrid_rrf   : BM25 + dense，RRF 融合（rerank=False）
                  hybrid_rerank: 在 hybrid_rrf 基础上加 cross-encoder 重排（需配置
                                 CHIP_RERANK_MODEL，否则该配置自动跳过并提示）

expected 字段按 type 约定：
    register: {"register_name": "...", "bit_field": "..."(可选)}
    figure:   {"caption_contains": "..."}
    prose:    {"section_contains": "..."}  （匹配 title/breadcrumb/text 任一）

用法：
    python eval/run_eval.py --kb ../data/knowledge.json --vectors ../data/vectors
    python eval/run_eval.py --questions questions.jsonl --top-k 10 --pool 30
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(Path(__file__).resolve().parent))
from validate_questions import load_and_validate  # noqa: E402


def _norm_bf(name: str) -> str:
    """归一化位域名：手册 OCR 偶把 '_' 识别成空格（如 'OVERFLOW_ERR' → 'OVERFLOW ERR'），
    比较时把两者都折叠成单一分隔符，避免因抽取噪声误判为未命中。"""
    return "_".join(name.lower().replace("_", " ").split())


def _rank_register(results: list[dict[str, Any]], expected: dict[str, Any]) -> int | None:
    want_reg = expected.get("register_name", "").lower()
    want_bf = _norm_bf(expected.get("bit_field", ""))
    for i, reg in enumerate(results):
        if want_reg and want_reg not in reg.get("register_name", "").lower():
            continue
        if want_bf:
            fields = reg.get("bit_fields", [])
            if not any(want_bf in _norm_bf(f.get("name", "")) for f in fields):
                continue
        return i + 1
    return None


def _rank_figure(figures: list[dict[str, Any]], expected: dict[str, Any]) -> int | None:
    want = expected.get("caption_contains", "").lower()
    for i, fig in enumerate(figures):
        if want in fig.get("caption", "").lower():
            return i + 1
    return None


def _rank_prose(results: list[dict[str, Any]], expected: dict[str, Any]) -> int | None:
    want = expected.get("section_contains", "").lower()
    for i, r in enumerate(results):
        hay = f"{r.get('title', '')} {r.get('breadcrumb', '')} {r.get('text', '')}".lower()
        if want in hay:
            return i + 1
    return None


def _hit(rank: int | None, k: int) -> bool:
    return rank is not None and rank <= k


class _Stats:
    def __init__(self) -> None:
        self.total = 0
        self.hit1 = 0
        self.hit5 = 0
        self.details: list[tuple[str, int | None]] = []

    def add(self, qid: str, rank: int | None) -> None:
        self.total += 1
        self.hit1 += int(_hit(rank, 1))
        self.hit5 += int(_hit(rank, 5))
        self.details.append((qid, rank))

    def summary(self) -> str:
        if self.total == 0:
            return "无题目"
        return f"hit@1={self.hit1}/{self.total} ({self.hit1/self.total:.0%})  hit@5={self.hit5}/{self.total} ({self.hit5/self.total:.0%})"


def main(argv: list[str] | None = None) -> int:
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8")  # type: ignore[attr-defined]
        except (AttributeError, ValueError):
            pass

    p = argparse.ArgumentParser(description="knowledge base 检索评测（hit@1/hit@5）。")
    p.add_argument("--questions", default=str(Path(__file__).parent / "questions.local.jsonl"),
                   help="本地 gold JSONL（默认 eval/questions.local.jsonl，已 gitignore）")
    p.add_argument("--kb", default=str(ROOT / "data" / "knowledge.json"))
    p.add_argument("--vectors", default=str(ROOT / "data" / "vectors"))
    p.add_argument("--top-k", type=int, default=10, help="prose 检索取多少条用于算 rank（>=5）")
    p.add_argument("--pool", type=int, default=30)
    args = p.parse_args(argv)

    os.environ["CHIP_KB_PATH"] = args.kb
    sys.path.insert(0, str(ROOT / "server"))
    import chip_server  # noqa: E402
    from retrieval import HybridIndex  # noqa: E402

    kb_for_validation = json.loads(Path(args.kb).read_text(encoding="utf-8"))
    questions, question_errors = load_and_validate(Path(args.questions), kb_for_validation)
    if question_errors:
        for error in question_errors:
            print(f"ERROR: {error}", file=sys.stderr)
        return 2
    by_type: dict[str, int] = {}
    for q in questions:
        by_type[q["type"]] = by_type.get(q["type"], 0) + 1
    print(f"题目共 {len(questions)} 条，按类型：{by_type}\n")

    # -- 基线：register / figure（结构化/关键词，不受 search_prose 算法影响）--------
    reg_stats, fig_stats = _Stats(), _Stats()
    for q in questions:
        keyword = q.get("keyword", q["query"])  # 未提供 keyword 时退化为整句（预期会大概率不中）
        if q["type"] == "register":
            res = chip_server.search_registers(keyword, q.get("module", ""))["results"]
            rank = _rank_register(res, q["expected"])
            reg_stats.add(q["id"], rank)
        elif q["type"] == "figure":
            res = chip_server.search_concepts(keyword, q.get("module", ""))["figures"]
            rank = _rank_figure(res, q["expected"])
            fig_stats.add(q["id"], rank)

    print("=== 基线（不受 search_prose 算法影响）===")
    print(f"search_registers: {reg_stats.summary()}")
    for qid, rank in reg_stats.details:
        print(f"  {qid}: rank={rank}")
    print(f"search_concepts(figures): {fig_stats.summary()}")
    for qid, rank in fig_stats.details:
        print(f"  {qid}: rank={rank}")

    # -- search_prose 算法对比：bm25 / hybrid_rrf / hybrid_rerank ------------------
    prose_questions = [q for q in questions if q["type"] == "prose"]
    idx = HybridIndex(Path(args.vectors))
    configs = [
        ("bm25", {"use_dense": False, "rerank": False}),
        ("hybrid_rrf", {"use_dense": True, "rerank": False}),
        ("hybrid_rerank", {"use_dense": True, "rerank": True}),
    ]

    print(f"\n=== search_prose 算法对比（{len(prose_questions)} 条 prose 题，top_k={args.top_k}）===")
    for name, kwargs in configs:
        stats = _Stats()
        note_seen = ""
        for q in prose_questions:
            res, note = idx.search(q["query"], q.get("module", ""), top_k=args.top_k,
                                    pool=args.pool, **kwargs)
            note_seen = note
            rank = _rank_prose(res, q["expected"])
            stats.add(q["id"], rank)
        skipped = "+rerank" not in note_seen and name == "hybrid_rerank"
        tag = "（未配置 CHIP_RERANK_MODEL，等同 hybrid_rrf）" if skipped else ""
        print(f"\n[{name}]{tag} {stats.summary()}")
        for qid, rank in stats.details:
            print(f"  {qid}: rank={rank}")

    return 0


if __name__ == "__main__":
    sys.exit(main())
