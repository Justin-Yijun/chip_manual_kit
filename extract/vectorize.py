#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""为 knowledge.json 构建结构感知的检索索引（本地 CPU 嵌入 + 混合检索语料）。

产物写入 data/vectors/：
    chunks.json      每个 chunk 的结构化元数据（module/kind/section_id/title/page/
                     breadcrumb/text/embed_text），供 BM25 词法检索与结果展示。
    embeddings.npy   与 chunks.json 行对齐的 dense 句向量（float32, L2 归一化）。
    meta.json        自描述元数据（模型、维度、分块参数、query_prefix），使任意
                     工具（Cursor / VS Code / Claude Code）都能按同一配置复现。

分块策略（本次改造）：
    * 结构感知：一个 section / 表 / 寄存器 / 枚举 / 算法尽量作为**一个** chunk，不再
      被固定窗口切碎；仅当单元超过 _MAX_CHARS 才按句界带重叠拆分。
    * 面包屑：每个 chunk 的 embed_text 前面拼上「module › 章节/单元标题」，补足上下文，
      让嵌入与 BM25 都能利用层级信息。
    * 语料更全：不仅 prose，还纳入 registers/enums/tables，让「既含标识符又含概念」
      的查询可被 dense+BM25 混合命中（精确寄存器仍可走 search_registers）。

模型：默认 BAAI/bge-small-en-v1.5（英文、~130MB、CPU 友好）。离线时先用 modelscope
下好模型到本地目录，再用 --model <本地路径> 或 CHIP_EMBED_MODEL 指向。

用法：
    python vectorize.py --kb ../data/knowledge.json --out ../data/vectors
    python vectorize.py --kb ../data/knowledge.json --out ../data/vectors --model /path/to/bge-small-en-v1.5
"""

import argparse
import json
import os
import re
import sys
from pathlib import Path
from typing import Any, Iterable

# 分块参数（字符级近似；英文 ~800 字符约 150 词）。写入 meta.json 以便复现。
_MAX_CHARS = 900
_OVERLAP = 150
_MIN_CHARS = 40
_BREADCRUMB_SEP = " › "


def _default_model() -> str:
    return os.environ.get("CHIP_EMBED_MODEL", "BAAI/bge-small-en-v1.5")


def _clean(text: str) -> str:
    """去 Markdown 转义/标题井号，压缩空白。"""
    text = re.sub(r"\\([_*#`\[\]()])", r"\1", text or "")
    text = re.sub(r"(^|\s)#{1,6}\s+", r"\1", text)
    return re.sub(r"\s+", " ", text).strip()


def _split_long(text: str) -> list[str]:
    """仅对超长单元按句界带重叠拆分；短单元原样保留（结构感知）。"""
    if len(text) <= _MAX_CHARS:
        return [text] if len(text) >= _MIN_CHARS else []
    chunks: list[str] = []
    start = 0
    while start < len(text):
        end = min(start + _MAX_CHARS, len(text))
        if end < len(text):
            cut = max(text.rfind(". ", start, end), text.rfind("; ", start, end))
            if cut > start + _MIN_CHARS:
                end = cut + 1
        piece = text[start:end].strip()
        if len(piece) >= _MIN_CHARS:
            chunks.append(piece)
        start = end - _OVERLAP if end - _OVERLAP > start else end
    return chunks


class _ChunkBuilder:
    def __init__(self) -> None:
        self.chunks: list[dict[str, Any]] = []

    def add(self, *, module: str, kind: str, title: str, text: str,
            section_id: str = "", page: Any = None) -> None:
        """把一个结构单元加入语料：整体作为一个 chunk，超长才拆，每片带面包屑。"""
        body = _clean(text)
        if len(body) < _MIN_CHARS:
            return
        title = _clean(title)
        crumbs = _BREADCRUMB_SEP.join(x for x in (module, title) if x)
        for piece in _split_long(body):
            embed_text = f"{crumbs}{_BREADCRUMB_SEP}{piece}" if crumbs else piece
            self.chunks.append({
                "id": f"{module}:{kind}:{section_id}:{page}:{len(self.chunks)}",
                "module": module,
                "kind": kind,
                "section_id": section_id,
                "title": title,
                "page": page,
                "breadcrumb": crumbs,
                "text": piece,          # 展示用（不含面包屑）
                "embed_text": embed_text,  # 嵌入/BM25 用（含面包屑）
            })


def _register_text(reg: dict[str, Any]) -> str:
    parts = [reg.get("register_name", ""), reg.get("address", ""), reg.get("description", "")]
    for f in reg.get("bit_fields", []):
        seg = " ".join(x for x in (
            f.get("bit_range", ""), f.get("name", ""), f.get("access", ""),
            f.get("reset_value", ""), f.get("description", "")) if x)
        if seg:
            parts.append(seg)
    return ". ".join(p for p in parts if p)


def _enum_text(enum: dict[str, Any]) -> str:
    parts = [enum.get("name", "")]
    for v in enum.get("values", []):
        seg = " ".join(x for x in (
            str(v.get("value", "")), v.get("mnemonic", ""), v.get("description", "")) if x)
        if seg:
            parts.append(seg)
    return ". ".join(p for p in parts if p)


def _table_text(tbl: dict[str, Any]) -> str:
    header = " | ".join(tbl.get("header", []))
    rows = [" | ".join(r) for r in tbl.get("rows", [])]
    body = "\n".join(rows)
    return (header + "\n" + body).strip() if header or body else ""


def _markdown_units(text: str) -> Iterable[tuple[str, str]]:
    """把 markdown 按标题切成 (heading, body) 单元，保留章节边界（结构感知）。"""
    heading = ""
    buf: list[str] = []
    for line in text.splitlines():
        ls = line.strip()
        m = re.match(r"^#{1,6}\s+(.*)$", ls)
        if m:
            if buf:
                yield heading, " ".join(buf)
                buf = []
            heading = m.group(1).strip()
            continue
        if not ls or ls.startswith("|") or ls.startswith("!") or ls.startswith("<"):
            continue
        buf.append(ls)
    if buf:
        yield heading, " ".join(buf)


def _norm_title(title: str) -> str:
    """归一化标题，用于判断 markdown 标题单元是否与某个结构化 section 重复。"""
    return re.sub(r"\s+", " ", (title or "").strip().lower())


def _build_chunks(kb: dict[str, Any], kb_dir: Path) -> list[dict[str, Any]]:
    b = _ChunkBuilder()

    # 记录每个模块已被结构化 section 覆盖的标题，供后面跳过 markdown 里的重复内容。
    covered_titles: dict[str, set[str]] = {}
    for s in kb.get("sections", []):
        b.add(module=s.get("module", ""), kind="section", title=s.get("title", ""),
              text=s.get("text", ""), section_id=s.get("section_id", ""), page=s.get("page"))
        norm = _norm_title(s.get("title", ""))
        if norm:
            covered_titles.setdefault(s.get("module", ""), set()).add(norm)
    for a in kb.get("algorithms", []):
        b.add(module=a.get("module", ""), kind="algorithm",
              title=a.get("caption", "") or a.get("sub_type", ""),
              text=a.get("text", ""), section_id=a.get("section_id", ""), page=a.get("page"))
    for reg in kb.get("registers", []):
        b.add(module=reg.get("module", ""), kind="register", title=reg.get("register_name", ""),
              text=_register_text(reg), section_id=reg.get("section_id", ""), page=reg.get("page"))
    for e in kb.get("enums", []):
        b.add(module=e.get("module", ""), kind="enum", title=e.get("name", ""),
              text=_enum_text(e), section_id=e.get("section_id", ""), page=e.get("page"))
    for t in kb.get("tables", []):
        b.add(module=t.get("module", ""), kind="table", title=t.get("caption", ""),
              text=_table_text(t), section_id=t.get("section_id", ""), page=t.get("page"))

    seen_md: set[str] = set()
    for doc in kb.get("documents", []):
        md_rel = doc.get("markdown_path", "")
        if not md_rel or md_rel in seen_md:
            continue
        seen_md.add(md_rel)
        md_file = kb_dir / md_rel
        if not md_file.exists():
            continue
        module = doc.get("module", "")
        text = md_file.read_text(encoding="utf-8", errors="ignore")
        covered = covered_titles.get(module, set())
        for heading, body in _markdown_units(text):
            # markdown 是全文扫描，标题命中的这一段内容与结构化 section 完全重复
            # （同一份 MinerU 解析产物，只是两种呈现形式）——重复 chunk 会在 BM25/dense
            # 各占一个候选名额，还会在 RRF 融合时让"两路都命中"的候选(如某个寄存器同时被
            # 关键词和语义命中)靠"重复计分"反超只在单路排名最高的正确答案。跳过重复，只保留
            # markdown 独有、未被结构化捕获的 prose（例如无标题的段落、未被识别为 section 的内容）。
            if heading and _norm_title(heading) in covered:
                continue
            b.add(module=module, kind="markdown", title=heading, text=body)

    return b.chunks


def main(argv=None) -> int:
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8")  # type: ignore[attr-defined]
        except (AttributeError, ValueError):
            pass

    p = argparse.ArgumentParser(description="构建结构感知的检索索引（嵌入 + 混合检索语料）。")
    p.add_argument("--kb", required=True, help="knowledge.json 路径")
    p.add_argument("--out", required=True, help="向量索引输出目录（如 data/vectors）")
    p.add_argument("--model", default=_default_model(), help="嵌入模型名或本地路径")
    p.add_argument("--batch", type=int, default=64)
    args = p.parse_args(argv)

    try:
        import numpy as np
        from sentence_transformers import SentenceTransformer
    except ImportError as exc:
        print(f"缺少依赖：{exc}。请在带 torch 的环境 pip install sentence-transformers。", file=sys.stderr)
        return 2

    kb_path = Path(args.kb)
    kb = json.loads(kb_path.read_text(encoding="utf-8"))
    chunks = _build_chunks(kb, kb_path.parent)
    if not chunks:
        print("没有可嵌入的 chunk。", file=sys.stderr)
        return 1

    kinds: dict[str, int] = {}
    for c in chunks:
        kinds[c["kind"]] = kinds.get(c["kind"], 0) + 1
    print(f"分块完成：{len(chunks)} 个 chunk，按类型 {kinds}")

    print(f"加载嵌入模型：{args.model}")
    model = SentenceTransformer(args.model, device="cpu")
    texts = [c["embed_text"] for c in chunks]
    print(f"嵌入 {len(texts)} 个 chunk ...")
    emb = model.encode(texts, batch_size=args.batch, normalize_embeddings=True,
                       show_progress_bar=True, convert_to_numpy=True).astype("float32")

    out_dir = Path(args.out)
    out_dir.mkdir(parents=True, exist_ok=True)
    np.save(out_dir / "embeddings.npy", emb)
    (out_dir / "chunks.json").write_text(json.dumps(chunks, ensure_ascii=False), encoding="utf-8")
    (out_dir / "meta.json").write_text(json.dumps({
        "model": args.model,
        "dim": int(emb.shape[1]),
        "count": len(chunks),
        "kinds": kinds,
        "query_prefix": "Represent this sentence for searching relevant passages: ",
        "chunking": {
            "structure_aware": True,
            "breadcrumb": True,
            "breadcrumb_sep": _BREADCRUMB_SEP,
            "max_chars": _MAX_CHARS,
            "overlap": _OVERLAP,
            "min_chars": _MIN_CHARS,
            "sources": ["sections", "algorithms", "registers", "enums", "tables", "markdown"],
        },
    }, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"已写出 {out_dir}: {len(chunks)} chunks, dim={emb.shape[1]}, model={args.model}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
