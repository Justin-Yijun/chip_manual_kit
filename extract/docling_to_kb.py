#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""可选后端：Docling PDF → MinerU 兼容 content_list → knowledge.json。

不替换 MinerU。Docling 作为对照/备选抽取器：产出与 MinerU 相同形状的
`*_content_list.json`，再复用 `mineru_to_kb._convert_doc` 进同一 schema。

用法：
    # PDF → 中间 content_list（可再喂给 mineru_to_kb / audit_source）
    python docling_to_kb.py --pdf manual.pdf --module ACME --out-dir out_docling

    # PDF 一键到 knowledge.json
    python docling_to_kb.py --pdf manual.pdf --module ACME --output data/kb_docling.json

    # 已有 Docling JSON（export_to_dict）→ content_list / knowledge.json（无需再跑 PDF）
    python docling_to_kb.py --docling-json doc.json --module ACME --out-dir out_docling

依赖（可选，未装则 CLI 会提示）：
    pip install docling
"""

from __future__ import annotations

import argparse
import html
import json
import sys
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent))
import mineru_to_kb as conv  # noqa: E402


def rows_to_html(rows: list[list[str]]) -> str:
    """二维单元格 → MinerU 风格 <table> HTML（首行当 th）。"""
    if not rows:
        return ""
    parts = ["<table>"]
    for i, row in enumerate(rows):
        parts.append("<tr>")
        tag = "th" if i == 0 else "td"
        for cell in row:
            parts.append(f"<{tag}>{html.escape(cell or '')}</{tag}>")
        parts.append("</tr>")
    parts.append("</table>")
    return "".join(parts)


def _page_idx_from_prov(item: Any) -> int:
    """Docling page_no 多为 1-based；MinerU content_list 用 0-based page_idx。"""
    prov = getattr(item, "prov", None) or []
    if not prov:
        return 0
    page_no = getattr(prov[0], "page_no", None)
    if page_no is None and isinstance(prov[0], dict):
        page_no = prov[0].get("page_no")
    try:
        n = int(page_no)
    except (TypeError, ValueError):
        return 0
    return max(0, n - 1)


def _label_name(item: Any) -> str:
    label = getattr(item, "label", None)
    if label is None:
        return ""
    return str(getattr(label, "value", label)).lower().replace("_", "-")


def _table_rows_from_item(item: Any) -> list[list[str]]:
    """从 Docling TableItem 抽出二维文本。优先 dataframe，其次 grid。"""
    data = getattr(item, "data", None)
    if data is None:
        return []
    # export_to_dataframe（若可用）
    export_df = getattr(item, "export_to_dataframe", None)
    if callable(export_df):
        try:
            df = export_df()
            header = [str(c) for c in df.columns.tolist()]
            body = [[("" if v is None else str(v)) for v in row] for row in df.values.tolist()]
            # Docling 有时把首行当数据而非列名；若列名是 RangeIndex 则用首行
            if header and all(str(h).isdigit() or str(h).startswith("Unnamed") for h in header):
                return body
            return [header, *body]
        except Exception:  # noqa: BLE001 - 回退 grid
            pass
    grid = getattr(data, "grid", None)
    if not grid:
        return []
    rows: list[list[str]] = []
    for row in grid:
        cells: list[str] = []
        for cell in row:
            text = getattr(cell, "text", None)
            if text is None and isinstance(cell, dict):
                text = cell.get("text", "")
            cells.append("" if text is None else str(text))
        if any(c.strip() for c in cells):
            rows.append(cells)
    return rows


def _picture_caption(item: Any) -> str:
    captions = getattr(item, "captions", None) or []
    texts: list[str] = []
    for cap in captions:
        t = getattr(cap, "text", None)
        if t is None and isinstance(cap, dict):
            t = cap.get("text")
        if t:
            texts.append(str(t))
    if texts:
        return " ".join(texts)
    # 部分版本用 caption 单字段
    cap = getattr(item, "caption", None)
    if cap is not None:
        return str(getattr(cap, "text", cap) or "")
    return ""


def docling_document_to_content_list(doc: Any) -> list[dict[str, Any]]:
    """把 DoclingDocument 映射为 MinerU 兼容 content_list 块序列。

    不依赖 Docling 安装以外的类型：用 duck-typing，便于单测喂假对象。
    """
    blocks: list[dict[str, Any]] = []
    iterate = getattr(doc, "iterate_items", None)
    if not callable(iterate):
        raise TypeError("doc 需提供 iterate_items()（DoclingDocument）")

    for item, level in iterate():
        label = _label_name(item)
        page = _page_idx_from_prov(item)
        text = str(getattr(item, "text", "") or "")

        if label in ("page-header", "page_header", "header"):
            blocks.append({"type": "header", "text": text, "page_idx": page})
            continue
        if label in ("page-footer", "page_footer", "footer"):
            blocks.append({"type": "footer", "text": text, "page_idx": page})
            continue

        # 表格：仅按 label / 类型名识别，避免普通 TextItem 误入
        if label == "table" or type(item).__name__ == "TableItem":
            rows = _table_rows_from_item(item)
            if rows:
                blocks.append({
                    "type": "table",
                    "table_body": rows_to_html(rows),
                    "page_idx": page,
                })
            continue

        is_picture = label in ("picture", "image", "chart") or type(item).__name__ == "PictureItem"
        if is_picture:
            img_path = ""
            image = getattr(item, "image", None)
            if image is not None:
                uri = getattr(image, "uri", None) or getattr(image, "path", None)
                if uri:
                    img_path = str(uri)
            blocks.append({
                "type": "image",
                "image_caption": [_picture_caption(item)],
                "img_path": img_path,
                "page_idx": page,
            })
            continue

        if label in ("formula", "equation"):
            if text.strip():
                blocks.append({"type": "equation", "text": text, "page_idx": page})
            continue

        if label in ("code",):
            if text.strip():
                blocks.append({
                    "type": "code",
                    "sub_type": "code",
                    "code_body": text,
                    "page_idx": page,
                })
            continue

        if label in ("title", "section-header", "section_header"):
            text_level = getattr(item, "level", None)
            if text_level is None:
                text_level = level if isinstance(level, int) and level > 0 else 1
            if text.strip():
                blocks.append({
                    "type": "text",
                    "text": text,
                    "text_level": int(text_level),
                    "page_idx": page,
                })
            continue

        if label in ("caption",):
            # 图题常已挂在 PictureItem.captions；独立 caption 块并入正文以免丢字
            if text.strip():
                blocks.append({"type": "text", "text": text, "page_idx": page})
            continue

        # 默认正文：paragraph / text / list_item / footnote 等
        if text.strip():
            blocks.append({"type": "text", "text": text, "page_idx": page})

    return blocks


def convert_pdf_with_docling(pdf: Path) -> Any:
    """调用 Docling DocumentConverter；未安装时给出明确错误。"""
    try:
        from docling.document_converter import DocumentConverter
    except ImportError as exc:  # pragma: no cover - 环境相关
        raise SystemExit(
            "未安装 docling。可选安装：pip install docling\n"
            "默认抽取后端仍是 MinerU；Docling 仅作对照。"
        ) from exc
    converter = DocumentConverter()
    result = converter.convert(str(pdf))
    return result.document


def load_docling_json(path: Path) -> Any:
    """从 Docling export_to_dict / save_as_json 产物加载 DoclingDocument。"""
    try:
        from docling_core.types.doc import DoclingDocument
    except ImportError as exc:  # pragma: no cover
        raise SystemExit(
            "加载 --docling-json 需要 docling-core：pip install docling"
        ) from exc
    data = json.loads(path.read_text(encoding="utf-8"))
    if hasattr(DoclingDocument, "model_validate"):
        return DoclingDocument.model_validate(data)
    return DoclingDocument.parse_obj(data)  # pydantic v1 fallback


def write_content_list(out_dir: Path, module: str, blocks: list[dict[str, Any]]) -> Path:
    out_dir.mkdir(parents=True, exist_ok=True)
    path = out_dir / f"{module.upper()}_content_list.json"
    path.write_text(json.dumps(blocks, ensure_ascii=False, indent=2), encoding="utf-8")
    return path


def build_knowledge(
    module_blocks: list[tuple[str, list[dict[str, Any]]]],
    output: Path,
    profile: Path | None = None,
) -> dict[str, Any]:
    conv._apply_profile(profile)
    kb: dict[str, Any] = {
        "modules": [],
        "documents": [],
        "registers": [],
        "sections": [],
        "enums": [],
        "formulas": [],
        "algorithms": [],
        "figures": [],
        "tables": [],
    }
    for module, blocks in module_blocks:
        part = conv._convert_doc(blocks, module.upper())
        if module.upper() not in kb["modules"]:
            kb["modules"].append(module.upper())
        kb["documents"].append({
            "module": module.upper(),
            "source": "docling",
            "blocks": len(blocks),
        })
        for key in ("registers", "sections", "enums", "formulas", "algorithms", "figures", "tables"):
            kb[key].extend(part[key])
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(kb, ensure_ascii=False, indent=2), encoding="utf-8")
    return kb


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description="Docling（可选）→ MinerU 兼容 content_list / knowledge.json"
    )
    src = p.add_mutually_exclusive_group(required=True)
    src.add_argument("--pdf", help="输入 PDF 路径")
    src.add_argument("--docling-json", help="已导出的 DoclingDocument JSON")
    p.add_argument("--module", required=True, help="模块名（写入 content_list 文件名与 KB）")
    p.add_argument("--out-dir", help="写出 MODULE_content_list.json 的目录")
    p.add_argument("--output", help="写出 knowledge.json 路径")
    p.add_argument("--profile", help="厂商抽取 profile（同 mineru_to_kb）")
    return p.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8")  # type: ignore[attr-defined]
        except (AttributeError, ValueError):
            pass

    args = _parse_args(argv)
    if not args.out_dir and not args.output:
        print("请至少指定 --out-dir 或 --output", file=sys.stderr)
        return 2

    if args.pdf:
        doc = convert_pdf_with_docling(Path(args.pdf))
    else:
        doc = load_docling_json(Path(args.docling_json))

    blocks = docling_document_to_content_list(doc)
    module = args.module.upper()
    print(f"Docling → content_list: {len(blocks)} blocks, module={module}")

    if args.out_dir:
        path = write_content_list(Path(args.out_dir), module, blocks)
        print(f"已写 {path}")

    if args.output:
        profile = Path(args.profile) if args.profile else None
        kb = build_knowledge([(module, blocks)], Path(args.output), profile)
        print(
            f"已写 {args.output}: registers={len(kb['registers'])} "
            f"sections={len(kb['sections'])} enums={len(kb['enums'])} "
            f"figures={len(kb['figures'])}"
        )
    return 0


if __name__ == "__main__":
    sys.exit(main())
