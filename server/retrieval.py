#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""混合检索：BM25 词法 + dense 向量，可选融合策略，可选 cross-encoder 重排。

设计要点（针对「返回给 agent 的上下文要少而精」）：
    * 每路（BM25 / dense）各取 candidate_pool（默认 30）。
    * 默认 Reciprocal Rank Fusion（RRF, k=60）把两路排名融合成一个候选序。
    * 可选 relative_score：按各路相对最高分归一后加权求和（LlamaIndex 风格 A/B）。
    * 可选 top-rank bonus：只要某文档在任一路排第 1 / 第 2-3，就加一次固定分数
      （借鉴开源项目 qmd 的 RRF 设计），防止多路融合把某一路的精确命中稀释掉——
      比如 BM25 完整命中标识符排第一，但 dense 语义泛化把它压到第 8，融合后被
      "两路都还行"的候选反超。默认关闭（两个 bonus 值都是 0），需显式开启。
    * 可选：用本地 CPU 的 cross-encoder（bge-reranker-base）对候选精排。
    * **只返回 top_k（默认 5）**，融合/重排全在服务端完成，不把几十条粗召回塞回上下文。

环境变量：
    CHIP_FUSION=rrf|relative_score        默认 rrf
    CHIP_FUSION_DENSE_WEIGHT=0.5          relative_score 时 dense 权重
    CHIP_FUSION_BM25_WEIGHT=0.5           relative_score 时 BM25 权重
    CHIP_FUSION_BONUS_RANK1=0             任一路排第 1 的 bonus，默认 0（关闭）
    CHIP_FUSION_BONUS_RANK2_3=0           任一路排第 2-3 的 bonus，默认 0（关闭）

全部惰性加载并优雅降级：
    * 缺 numpy/sentence-transformers 或缺 embeddings → 退化为纯 BM25。
    * 缺 reranker 模型 → 跳过重排，用融合结果。
    * 连 chunks.json 都没有 → 返回空并在 note 说明。

依赖：BM25 为纯标准库实现（离线、可复现）；dense 用 sentence-transformers；
reranker 用 sentence-transformers 的 CrossEncoder（可选）。
"""

from __future__ import annotations

import json
import math
import os
import re
from pathlib import Path
from typing import Any

_RRF_K = 60
_DEFAULT_POOL = 30
_TOKEN_RE = re.compile(r"[a-z0-9]+(?:_[a-z0-9]+)*", re.IGNORECASE)
_VOWELS = frozenset("aeiou")
_NO_STEM = frozenset({
    "does", "this", "was", "has", "his", "its", "ours", "theirs", "yes",
})


def _light_stem(word: str) -> str:
    """保守的英文轻量词干归一（无第三方依赖）。

    只处理普通纯字母词的常见屈折后缀；寄存器标识符、数字、短词和少量易误判词不处理。
    调用方会完整保留寄存器标识符，只归一普通词和标识符子词。
    """
    if len(word) < 4 or not word.isalpha() or word in _NO_STEM:
        return word

    stem = word
    if len(word) > 5 and word.endswith("ies"):
        stem = word[:-3] + "y"
    elif len(word) > 5 and word.endswith("ing"):
        stem = word[:-3]
    elif len(word) > 4 and word.endswith("ed"):
        stem = word[:-2]
    elif len(word) > 4 and word.endswith("es") and not word.endswith(("ses", "xes")):
        stem = word[:-2]
    elif len(word) > 3 and word.endswith("s") and not word.endswith(("ss", "us", "is")):
        stem = word[:-1]
    elif len(word) > 4 and word.endswith("e") and not word.endswith("ee"):
        stem = word[:-1]

    # dropped/dropping -> dropp -> drop；buffered -> bufferr 不会发生（只双写末尾辅音）。
    if len(stem) > 3 and stem[-1] == stem[-2] and stem[-1] not in _VOWELS:
        stem = stem[:-1]
    return stem


def _append_token_with_stem(tokens: list[str], token: str) -> None:
    stem = _light_stem(token)
    tokens.append(stem)


def _tokenize(text: str) -> list[str]:
    """标识符感知分词；普通英文词统一为轻量词干。

    标识符整体保持原样，其子词做归一。普通词用 stem 替换原词，而非同时保留两份，
    避免人为增加文档长度/词频、扭曲 BM25 排名。
    """
    toks: list[str] = []
    for m in _TOKEN_RE.findall((text or "").lower()):
        # 标识符整体必须原样保留，避免 RX_STAT4 等被词干化。
        toks.append(m)
        if "_" in m:
            for part in m.split("_"):
                if part:
                    _append_token_with_stem(toks, part)
        else:
            toks[-1] = _light_stem(m)
    return toks


class _BM25:
    """Okapi BM25（纯 Python，离线可复现）。"""

    def __init__(self, corpus_tokens: list[list[str]], k1: float = 1.5, b: float = 0.75) -> None:
        self.k1 = k1
        self.b = b
        self.docs = corpus_tokens
        self.n = len(corpus_tokens)
        self.doc_len = [len(d) for d in corpus_tokens]
        self.avgdl = (sum(self.doc_len) / self.n) if self.n else 0.0
        self.tf: list[dict[str, int]] = []
        df: dict[str, int] = {}
        for toks in corpus_tokens:
            freqs: dict[str, int] = {}
            for t in toks:
                freqs[t] = freqs.get(t, 0) + 1
            self.tf.append(freqs)
            for t in freqs:
                df[t] = df.get(t, 0) + 1
        self.idf: dict[str, float] = {
            t: math.log(1 + (self.n - d + 0.5) / (d + 0.5)) for t, d in df.items()
        }

    def top_n(self, query: str, n: int) -> list[tuple[int, float]]:
        q = _tokenize(query)
        if not q or self.n == 0:
            return []
        scores = [0.0] * self.n
        for i in range(self.n):
            freqs = self.tf[i]
            if not freqs:
                continue
            dl = self.doc_len[i]
            denom_dl = self.k1 * (1 - self.b + self.b * dl / self.avgdl) if self.avgdl else self.k1
            s = 0.0
            for t in q:
                f = freqs.get(t)
                if not f:
                    continue
                s += self.idf.get(t, 0.0) * (f * (self.k1 + 1)) / (f + denom_dl)
            scores[i] = s
        ranked = sorted(range(self.n), key=lambda i: scores[i], reverse=True)
        out = [(i, scores[i]) for i in ranked if scores[i] > 0]
        return out[:n]


class HybridIndex:
    """惰性加载的混合检索索引。首次查询时才载入 chunks / 向量 / 模型。"""

    def __init__(self, vectors_dir: Path) -> None:
        self._dir = vectors_dir
        self._loaded = False
        self._chunks: list[dict[str, Any]] = []
        self._bm25: _BM25 | None = None
        self._emb = None
        self._model = None
        self._query_prefix = ""
        self._reranker = None
        self._reranker_tried = False
        self._note = ""
        self._dense_ok = False

    # -- 加载 --------------------------------------------------------------
    def _load(self) -> None:
        if self._loaded:
            return
        self._loaded = True
        chunks_file = self._dir / "chunks.json"
        if not chunks_file.exists():
            self._note = f"未找到 {chunks_file}，请先运行 extract/vectorize.py 生成索引。"
            return
        self._chunks = json.loads(chunks_file.read_text(encoding="utf-8"))
        corpus = [_tokenize(c.get("embed_text") or c.get("text", "")) for c in self._chunks]
        self._bm25 = _BM25(corpus)

        # dense（可选）
        emb_file = self._dir / "embeddings.npy"
        meta_file = self._dir / "meta.json"
        if emb_file.exists():
            try:
                import numpy as np
                from sentence_transformers import SentenceTransformer
                meta = json.loads(meta_file.read_text(encoding="utf-8")) if meta_file.exists() else {}
                self._query_prefix = meta.get("query_prefix", "")
                model_name = meta.get("model") or os.environ.get(
                    "CHIP_EMBED_MODEL", "BAAI/bge-small-en-v1.5")
                self._emb = np.load(emb_file)
                self._model = SentenceTransformer(model_name, device="cpu")
                self._dense_ok = True
            except Exception as exc:  # noqa: BLE001 - 优雅降级为纯 BM25
                self._note = f"dense 检索不可用（{exc}），已降级为纯 BM25。"
        else:
            self._note = "未找到 embeddings.npy，已降级为纯 BM25（仅词法）。"

    def _ensure_reranker(self):
        if self._reranker_tried:
            return self._reranker
        self._reranker_tried = True
        model_path = os.environ.get("CHIP_RERANK_MODEL")
        if not model_path:
            return None
        try:
            from sentence_transformers import CrossEncoder
            self._reranker = CrossEncoder(model_path, device="cpu", max_length=512)
        except Exception:  # noqa: BLE001 - 缺模型/依赖则跳过重排
            self._reranker = None
        return self._reranker

    # -- 单路召回 ----------------------------------------------------------
    def _dense_top(self, query: str, pool: int) -> list[tuple[int, float]]:
        if not self._dense_ok:
            return []
        import numpy as np
        q = self._model.encode([self._query_prefix + query], normalize_embeddings=True,
                               convert_to_numpy=True).astype("float32")
        scores = self._emb @ q[0]
        order = np.argsort(-scores)[: pool * 2]
        return [(int(i), float(scores[int(i)])) for i in order]

    def _bm25_top(self, query: str, pool: int) -> list[tuple[int, float]]:
        return self._bm25.top_n(query, pool * 2) if self._bm25 else []

    # -- 融合 + 重排 -------------------------------------------------------
    @staticmethod
    def _apply_rank_bonus(
        fused: dict[int, float],
        best_rank: dict[int, int],
        bonus_rank1: float,
        bonus_rank2_3: float,
    ) -> None:
        """按"任一路最佳名次"给 fused 原地加 bonus（不按命中路数叠加，只加一次）。

        用于 _rrf / _relative_score 共享——两者语义一致：见调用方 docstring。
        """
        if not bonus_rank1 and not bonus_rank2_3:
            return
        for idx, rank in best_rank.items():
            if idx not in fused:
                continue
            if rank == 0:
                fused[idx] += bonus_rank1
            elif rank in (1, 2):
                fused[idx] += bonus_rank2_3

    @staticmethod
    def _rrf(
        *rankings: list[tuple[int, float]],
        bonus_rank1: float = 0.0,
        bonus_rank2_3: float = 0.0,
    ) -> dict[int, float]:
        """Reciprocal Rank Fusion（k=60），可选 top-rank bonus（默认关闭，见模块文档）。"""
        fused: dict[int, float] = {}
        best_rank: dict[int, int] = {}
        for ranking in rankings:
            for rank, (idx, _score) in enumerate(ranking):
                fused[idx] = fused.get(idx, 0.0) + 1.0 / (_RRF_K + rank + 1)
                if idx not in best_rank or rank < best_rank[idx]:
                    best_rank[idx] = rank
        HybridIndex._apply_rank_bonus(fused, best_rank, bonus_rank1, bonus_rank2_3)
        return fused

    @staticmethod
    def _relative_score(
        *rankings: list[tuple[int, float]],
        weights: list[float] | None = None,
        bonus_rank1: float = 0.0,
        bonus_rank2_3: float = 0.0,
    ) -> dict[int, float]:
        """各路分数除以该路最高分后加权求和（LlamaIndex RelativeScoreFusion 风格）。

        可选 top-rank bonus，默认关闭，语义同 _rrf。
        """
        active = [(r, w) for r, w in zip(rankings, weights or []) if r]
        if weights is None:
            n = sum(1 for r in rankings if r)
            w_each = (1.0 / n) if n else 1.0
            active = [(r, w_each) for r in rankings if r]
        fused: dict[int, float] = {}
        best_rank: dict[int, int] = {}
        for ranking, weight in active:
            max_s = max(s for _, s in ranking)
            if max_s <= 0:
                continue
            for rank, (idx, score) in enumerate(ranking):
                fused[idx] = fused.get(idx, 0.0) + weight * (score / max_s)
                if idx not in best_rank or rank < best_rank[idx]:
                    best_rank[idx] = rank
        HybridIndex._apply_rank_bonus(fused, best_rank, bonus_rank1, bonus_rank2_3)
        return fused

    @staticmethod
    def _resolve_fusion(fusion: str | None) -> str:
        name = (fusion or os.environ.get("CHIP_FUSION") or "rrf").strip().lower()
        if name in ("relative", "relative_score", "rel"):
            return "relative_score"
        return "rrf"

    @staticmethod
    def _resolve_bonus(
        bonus_rank1: float | None,
        bonus_rank2_3: float | None,
    ) -> tuple[float, float]:
        """显式参数优先；缺省则读 CHIP_FUSION_BONUS_RANK1/RANK2_3；两者都缺省时为 0（关闭）。"""
        if bonus_rank1 is None:
            try:
                bonus_rank1 = float(os.environ.get("CHIP_FUSION_BONUS_RANK1", "0"))
            except ValueError:
                bonus_rank1 = 0.0
        if bonus_rank2_3 is None:
            try:
                bonus_rank2_3 = float(os.environ.get("CHIP_FUSION_BONUS_RANK2_3", "0"))
            except ValueError:
                bonus_rank2_3 = 0.0
        return bonus_rank1, bonus_rank2_3

    def search(
        self,
        query: str,
        module: str,
        top_k: int,
        pool: int = _DEFAULT_POOL,
        rerank: bool = True,
        use_dense: bool = True,
        fusion: str | None = None,
        bonus_rank1: float | None = None,
        bonus_rank2_3: float | None = None,
    ) -> tuple[list[dict[str, Any]], str]:
        """use_dense=False 时强制只用 BM25（供评测对比 A/B，不需要另建索引）。

        fusion: rrf（默认）| relative_score；None 时读 CHIP_FUSION。
        bonus_rank1/bonus_rank2_3: top-rank bonus，None 时读
        CHIP_FUSION_BONUS_RANK1/CHIP_FUSION_BONUS_RANK2_3（默认均 0，即关闭，
        不改变现有默认排序）。
        """
        self._load()
        if not self._chunks:
            return [], self._note
        module_lc = module.strip().lower()
        fusion_mode = self._resolve_fusion(fusion)
        b1, b23 = self._resolve_bonus(bonus_rank1, bonus_rank2_3)

        dense = self._dense_top(query, pool) if use_dense else []
        lexical = self._bm25_top(query, pool)
        if fusion_mode == "relative_score" and dense and lexical:
            try:
                w_dense = float(os.environ.get("CHIP_FUSION_DENSE_WEIGHT", "0.5"))
                w_bm25 = float(os.environ.get("CHIP_FUSION_BM25_WEIGHT", "0.5"))
            except ValueError:
                w_dense, w_bm25 = 0.5, 0.5
            fused = self._relative_score(dense, lexical, weights=[w_dense, w_bm25],
                                          bonus_rank1=b1, bonus_rank2_3=b23)
            fuse_tag = "relative_score(bm25+dense)"
        else:
            fused = self._rrf(dense, lexical, bonus_rank1=b1, bonus_rank2_3=b23)
            fuse_tag = "rrf(bm25+dense)"
        if not fused:
            return [], self._note or "无匹配结果。"

        dense_score = {i: s for i, s in dense}
        bm25_score = {i: s for i, s in lexical}
        ordered = sorted(fused.items(), key=lambda kv: kv[1], reverse=True)

        # 模块过滤 + 收集候选池（用于可选重排）
        cand: list[tuple[int, float]] = []
        for idx, fscore in ordered:
            ch = self._chunks[idx]
            if module_lc and module_lc not in ch.get("module", "").lower():
                continue
            cand.append((idx, fscore))
            if len(cand) >= pool:
                break
        if not cand:
            return [], "该模块下无匹配结果，可去掉 module 再试。"

        method = fuse_tag if (self._dense_ok and use_dense and dense) else "bm25"
        if method != "bm25" and (b1 or b23):
            method += "+bonus"  # 提示这次融合分数里含 top-rank bonus，便于评测/调试对照
        reranker = self._ensure_reranker() if rerank else None
        if reranker is not None and len(cand) > 1:
            pairs = [(query, self._chunks[i].get("text", "")) for i, _ in cand]
            try:
                rr = reranker.predict(pairs)
                cand = [c for c, _ in sorted(zip(cand, rr), key=lambda x: x[1], reverse=True)]
                method += "+rerank"
            except Exception:  # noqa: BLE001 - 重排失败则保留融合顺序
                pass

        results: list[dict[str, Any]] = []
        for idx, fscore in cand[:top_k]:
            ch = self._chunks[idx]
            results.append({
                "module": ch.get("module", ""),
                "kind": ch.get("kind", ""),
                "title": ch.get("title", ""),
                "breadcrumb": ch.get("breadcrumb", ""),
                "page": ch.get("page"),
                "section_id": ch.get("section_id", ""),
                "text": ch.get("text", ""),
                "fusion_score": round(fscore, 5),
                "rrf_score": round(fscore, 5),  # 兼容旧字段名
                "dense_score": round(dense_score.get(idx, 0.0), 4),
                "bm25_score": round(bm25_score.get(idx, 0.0), 4),
            })
        note = self._note or ""
        note = (note + " " if note else "") + f"method={method}, pool={len(cand)}, returned={len(results)}"
        return results, note.strip()
