#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""MinerU 输出 → 芯片编程知识库(knowledge.json)。

读取 MinerU pipeline 产出的 `*_content_list.json`（每个 PDF 一个），按阅读顺序
重建结构化知识库，替代旧的 pdfplumber 版 registers.json。相比旧管线，额外产出：
    - registers：位域表新增 access / reset_value / typical_value 列
    - enums    ：枚举表（Value / Mnemonic / Description），对应 C 里的 *_E 常量
    - formulas ：MinerU 公式模型识别出的 LaTeX 行间公式
    - algorithms：伪代码/算法块（如 SLOT_ID 计算）
    - figures  ：抽取出的框图/图片及其标题
    - sections ：带层级的章节解说正文（显式 section_id 外键）

用法：
    python mineru_to_kb.py --input out_acme out_other --output knowledge.json
其中每个 --input 目录会被递归查找 `*_content_list.json`。
"""

import argparse
import html
import json
import re
import shutil
import sys
import unicodedata
from pathlib import Path
from typing import Any

from bs4 import BeautifulSoup

# ---------------------------------------------------------------------------
# 常量
# ---------------------------------------------------------------------------

# 需整体丢弃的版面块类型（页眉/页脚/侧边水印文字）
_DROP_BLOCK_TYPES = {"header", "footer", "aside_text"}

# 水印/版权样板 token（大小写不敏感子串匹配，命中则从文本中剔除该行）
_NOISE_SUBSTRINGS = (
    "copyright",
    "proprietary",
    "confidential",
    "document classification",
)

# 寄存器名：全大写字母/数字/下划线，允许 (0..2) 实例范围（含 OCR 的 O→0 容错）
_RE_REGISTER_NAME = re.compile(r"^[A-Z][A-Z0-9_]*(?:\([0-9.]+\)[A-Z0-9_]*)*$")
_REGISTER_REQUIRE_UNDERSCORE = True
_ENUM_SUFFIXES: tuple[str, ...] = ("_E", "_S")

# 厂商手册常用表头别名。可通过 --profile JSON 扩展/覆盖，以适配其它厂商的列名。
_HEADER_ALIASES: dict[str, list[str]] = {
    "bit_position": ["bit pos"],
    "field_name": ["field name"],
    "register": ["register"],
    "address": ["address", "bar"],
    "mnemonic": ["mnemonic"],
    "value": ["value", "enum"],
    "access": ["access"],
    "reset": ["reset value", "reset"],
    "typical": ["typical value", "typical"],
    "description": ["field description", "description"],
}

# 位域位置：默认尖括号风格 <63:48> / <0>；profile 可追加 [7:0]、15:0 等格式。
_BIT_POSITION_PATTERNS: list[re.Pattern[str]] = [
    re.compile(r"^<\d+(?::\d+)?>$")
]

# 十六进制地址
_RE_HEX = re.compile(r"0x[0-9A-Fa-f]+")

# PDF 抽取常见粘连：说明文字与紧跟的寄存器名之间丢失空格，如
# "...RegistersRFOE(0..6)_RX_APERT_SMEM_MIN"。合法寄存器名全大写、不含小写字母，
# 因此在"小写字母→大写字母"处补切一刀总是安全的：不会切碎任何真正的寄存器名，
# 只会切开"描述文字+紧跟着的寄存器名"这种粘连。
_RE_LOWER_TO_UPPER_BOUNDARY = re.compile(r"(?<=[a-z])(?=[A-Z])")


def _apply_profile(path: Path | None) -> None:
    """加载可选厂商适配 profile；未提供时保持当前行为完全不变。

    支持字段：
      register_name_pattern: Python regex（整串匹配寄存器 token）
      noise_substrings:      追加要清理的水印/版权子串
      table_header_aliases:  {语义列名: [厂商表头别名]}，追加到默认别名
      register_require_underscore: 是否要求寄存器名含下划线（默认 true）
      bit_position_patterns: 位域位置 regex 列表（追加到默认 <n:m>）
      enum_suffixes:         枚举类型名后缀（替换默认 _E/_S）
    """
    if path is None:
        return
    profile = json.loads(path.read_text(encoding="utf-8"))
    global _RE_REGISTER_NAME, _NOISE_SUBSTRINGS
    global _REGISTER_REQUIRE_UNDERSCORE, _ENUM_SUFFIXES
    pattern = profile.get("register_name_pattern")
    if pattern:
        _RE_REGISTER_NAME = re.compile(pattern)
    if "register_require_underscore" in profile:
        _REGISTER_REQUIRE_UNDERSCORE = bool(profile["register_require_underscore"])
    for pattern_text in profile.get("bit_position_patterns", []):
        compiled = re.compile(str(pattern_text))
        if compiled.pattern not in {x.pattern for x in _BIT_POSITION_PATTERNS}:
            _BIT_POSITION_PATTERNS.append(compiled)
    if profile.get("enum_suffixes"):
        _ENUM_SUFFIXES = tuple(str(x) for x in profile["enum_suffixes"])
    noise = [str(x).lower() for x in profile.get("noise_substrings", [])]
    if noise:
        _NOISE_SUBSTRINGS = (*_NOISE_SUBSTRINGS, *noise)
    for key, values in profile.get("table_header_aliases", {}).items():
        if key not in _HEADER_ALIASES:
            raise ValueError(
                f"profile.table_header_aliases 未知键 {key!r}；允许：{', '.join(sorted(_HEADER_ALIASES))}"
            )
        for value in values:
            alias = str(value).lower()
            if alias not in _HEADER_ALIASES[key]:
                _HEADER_ALIASES[key].append(alias)


def _aliases(key: str) -> tuple[str, ...]:
    return tuple(_HEADER_ALIASES[key])


# ---------------------------------------------------------------------------
# 文本清洗
# ---------------------------------------------------------------------------

def _clean_text(text: Any) -> str:
    """清洗 PUA 字形、替换符、控制字符，并做 HTML 实体反转义与空白归一。"""
    if not text:
        return ""
    text = html.unescape(str(text))
    # 去除 Markdown 转义反斜杠（MinerU 可能把 ACME_CTRL 写成 ACME\_CTRL）
    text = re.sub(r"\\([_*#`\[\]()])", r"\1", text)
    cleaned: list[str] = []
    for ch in text:
        if "\ue000" <= ch <= "\uf8ff":  # 私有使用区
            continue
        if ch == "\ufffd":
            continue
        if unicodedata.category(ch).startswith("C") and ch not in "\n\t":
            continue
        cleaned.append(ch)
    out = re.sub(r"\s+", " ", "".join(cleaned)).strip()
    return out


def _strip_watermark(text: str) -> str:
    """从单元格/正文里剔除混入的水印 token。"""
    if not text:
        return ""
    low = text.lower()
    if any(sub in low for sub in _NOISE_SUBSTRINGS):
        # 逐 token 过滤：去掉包含噪声子串的连续大写水印词
        tokens = text.split()
        kept = [t for t in tokens if not any(sub in t.lower() for sub in _NOISE_SUBSTRINGS)]
        text = " ".join(kept)
    return text


def _normalize_ocr(name: str) -> str:
    """修正常见 OCR 误识：实例范围里的 O→0（如 (O..1) → (0..1)）。"""
    return re.sub(r"\(O\.\.", "(0..", name)


def _section_id(module: str, title: str) -> str:
    """由模块名与章节标题派生稳定的显式外键 id。"""
    if not title:
        return ""
    slug = re.sub(r"[^a-z0-9]+", "-", title.lower()).strip("-")
    return f"{module.lower()}:{slug}"[:120]


# ---------------------------------------------------------------------------
# HTML 表格解析
# ---------------------------------------------------------------------------

def _table_rows(table_html: str) -> list[list[str]]:
    """把 <table> HTML 解析成二维字符串数组（已清洗）。"""
    soup = BeautifulSoup(table_html or "", "html.parser")
    rows: list[list[str]] = []
    for tr in soup.find_all("tr"):
        cells = [_strip_watermark(_clean_text(td.get_text(" "))) for td in tr.find_all(["td", "th"])]
        if any(cells):
            rows.append(cells)
    return rows


def _find_col(header: list[str], *keys: str) -> int:
    """在表头里找列索引（大小写不敏感），按 keys 的优先顺序匹配。

    先用第一个 key 扫一遍所有列，找到就返回；找不到才试下一个 key。
    不能按"列顺序优先"扫（对每列判断是否命中任一 key）：比如
    ["Register","BAR","Address","CSRType"] 这种表头，"address" 是真正的
    地址列，但排序更靠后的"bar"列会在按列顺序扫描时被优先命中，
    导致地址列被 BAR（Base Address Register 索引，不是地址）抢走。
    """
    low = [h.lower() for h in header]
    for key in keys:
        for idx, cell in enumerate(low):
            if key in cell:
                return idx
    return -1


def _classify_table(header: list[str]) -> str:
    """按表头判定表类型：bitfield / summary / enum / other。"""
    joined = " ".join(h.lower() for h in header)
    if any(x in joined for x in _aliases("bit_position")) and any(
        x in joined for x in _aliases("field_name")
    ):
        return "bitfield"
    if any(x in joined for x in _aliases("register")) and any(
        x in joined for x in _aliases("address")
    ):
        return "summary"
    if any(x in joined for x in _aliases("mnemonic")) and any(
        x in joined for x in _aliases("value")
    ):
        return "enum"
    return "other"


def _parse_bitfields(rows: list[list[str]]) -> list[dict[str, str]]:
    """解析位域表，返回 [{name, bit_range, access, reset_value, typical_value, description}]。"""
    header = rows[0]
    i_bit = _find_col(header, *_aliases("bit_position"))
    i_name = _find_col(header, *_aliases("field_name"))
    i_acc = _find_col(header, *_aliases("access"))
    i_reset = _find_col(header, *_aliases("reset"))
    i_typ = _find_col(header, *_aliases("typical"))
    i_desc = _find_col(header, *_aliases("description"))

    def cell(row: list[str], idx: int) -> str:
        return row[idx] if 0 <= idx < len(row) else ""

    fields: list[dict[str, str]] = []
    for row in rows[1:]:
        bit_range = cell(row, i_bit)
        if not any(pattern.match(bit_range) for pattern in _BIT_POSITION_PATTERNS):
            continue
        fields.append(
            {
                "name": cell(row, i_name) or "RESERVED",
                "bit_range": bit_range,
                "access": cell(row, i_acc),
                "reset_value": cell(row, i_reset),
                "typical_value": cell(row, i_typ),
                "description": cell(row, i_desc),
            }
        )
    return fields


def _parse_summary(rows: list[list[str]]) -> dict[str, str]:
    """解析寄存器汇总表，返回 {寄存器名: 地址}。"""
    header = rows[0]
    i_name = _find_col(header, *_aliases("register"))
    i_addr = _find_col(header, *_aliases("address"))
    mapping: dict[str, str] = {}
    for row in rows[1:]:
        if not (0 <= i_name < len(row)):
            continue
        raw = _normalize_ocr(row[i_name])
        name = next((tok for tok in raw.split() if _RE_REGISTER_NAME.match(tok) and "_" in tok), "")
        if not name:
            continue
        addr = ""
        if 0 <= i_addr < len(row):
            m = _RE_HEX.search(row[i_addr])
            addr = m.group(0) if m else ""
        mapping[name] = addr
    return mapping


def _parse_enum(rows: list[list[str]]) -> list[dict[str, str]]:
    """解析枚举表，返回 [{value, mnemonic, description}]。"""
    header = rows[0]
    i_val = _find_col(header, *_aliases("value"))
    i_mn = _find_col(header, *_aliases("mnemonic"))
    i_desc = _find_col(header, *_aliases("description"))

    def cell(row: list[str], idx: int) -> str:
        return row[idx] if 0 <= idx < len(row) else ""

    values: list[dict[str, str]] = []
    for row in rows[1:]:
        mn = cell(row, i_mn)
        val = cell(row, i_val)
        if not mn and not val:
            continue
        values.append({"value": val, "mnemonic": mn, "description": cell(row, i_desc)})
    return values


# ---------------------------------------------------------------------------
# 单文档转换
# ---------------------------------------------------------------------------

def _is_register_name(text: str) -> bool:
    text = _normalize_ocr(text)
    has_required_separator = "_" in text or not _REGISTER_REQUIRE_UNDERSCORE
    return len(text) >= 4 and has_required_separator and bool(_RE_REGISTER_NAME.match(text))


def _extract_register_token(text: str) -> str:
    """从标题/正文里抽出寄存器名 token（容忍 "3.2 ACME_CTRL_STATUS" 等前缀）。

    MinerU 里寄存器名常作为带章节号的标题出现，整串不匹配寄存器名正则，
    因此按 token 扫描，取首个形如寄存器名（含下划线、可含 (0..2) 实例范围）者。
    若整个 token 不匹配，再尝试在"小写→大写"边界处二次拆分，兼容
    "DescriptionRegisters" 与紧跟寄存器名之间丢失空格的粘连情况。
    """
    for tok in _normalize_ocr(text).split():
        tok = tok.strip(".,:；;")
        if _is_register_name(tok):
            return tok
        for piece in _RE_LOWER_TO_UPPER_BOUNDARY.split(tok):
            if _is_register_name(piece):
                return piece
    return ""


# figure 关联的就近正文最大长度（够解释框图、又不撑大上下文）
_FIGURE_CONTEXT_MAXLEN = 600


def _convert_doc(content_list: list[dict[str, Any]], module: str) -> dict[str, Any]:
    """把单个 content_list 转成该模块的知识片段。"""
    registers: dict[str, dict[str, Any]] = {}
    sections: dict[str, dict[str, Any]] = {}
    enums: list[dict[str, Any]] = []
    formulas: list[dict[str, Any]] = []
    algorithms: list[dict[str, Any]] = []
    figures: list[dict[str, Any]] = []
    tables: list[dict[str, Any]] = []
    address_map: dict[str, str] = {}

    current_section: dict[str, Any] | None = None
    # 记录最近出现的寄存器名候选（标题或正文），用于关联随后的位域表
    last_register = ""

    for block in content_list:
        btype = block.get("type")
        if btype in _DROP_BLOCK_TYPES:
            continue
        page = block.get("page_idx")

        if btype == "text":
            text = _strip_watermark(_clean_text(block.get("text")))
            if not text:
                continue
            # 整串就是寄存器名：作为寄存器锚点，不进正文
            if _is_register_name(text):
                last_register = _normalize_ocr(text)
                continue
            if block.get("text_level"):  # 章节标题
                sid = _section_id(module, text)
                current_section = sections.setdefault(
                    sid,
                    {
                        "section_id": sid,
                        "module": module,
                        "title": text,
                        "level": block.get("text_level"),
                        "text": "",
                        "page": page,
                    },
                )
                # 标题里若含寄存器名 token（如 "3.2 ACME_CTRL_STATUS"），设为锚点
                token = _extract_register_token(text)
                if token:
                    last_register = token
                continue
            # 非标题正文：形如 "ACME Control Structure ACME_CTRL_S" 的短标注行也做锚点
            token = _extract_register_token(text)
            if token and len(text.split()) <= 8:
                last_register = token
                continue
            if current_section is not None:
                joined = f"{current_section['text']} {text}".strip()
                current_section["text"] = joined
            continue

        if btype == "table":
            rows = _table_rows(block.get("table_body", ""))
            if not rows:
                continue
            kind = _classify_table(rows[0])
            sid = current_section["section_id"] if current_section else ""
            title = current_section["title"] if current_section else ""
            if kind == "summary":
                address_map.update(_parse_summary(rows))
            elif kind == "bitfield":
                fields = _parse_bitfields(rows)
                if not last_register:
                    continue
                if last_register in registers:
                    registers[last_register]["bit_fields"].extend(fields)
                else:
                    registers[last_register] = {
                        "module": module,
                        "register_name": last_register,
                        "address": "",
                        "description": title,
                        "section_id": sid,
                        "bit_fields": fields,
                        "page": page,
                    }
            elif kind == "enum":
                # 枚举名优先取最近的 *_E 标识符 token，否则回退到章节标题
                enum_name = last_register if last_register.endswith(_ENUM_SUFFIXES) else (title or last_register)
                enums.append(
                    {
                        "module": module,
                        "name": enum_name,
                        "section_id": sid,
                        "values": _parse_enum(rows),
                        "page": page,
                    }
                )
            else:
                # 其它表（位布局/编码/包格式等）：保留结构化行，而非有损 flatten
                tables.append(
                    {
                        "module": module,
                        "section_id": sid,
                        "caption": title,
                        "header": rows[0],
                        "rows": rows[1:],
                        "page": page,
                    }
                )
            continue

        if btype == "equation":
            # MinerU 公式块：text/latex 字段任一
            latex = _clean_text(block.get("text") or block.get("latex"))
            if latex:
                formulas.append(
                    {
                        "module": module,
                        "section_id": current_section["section_id"] if current_section else "",
                        "latex": latex,
                        "page": page,
                    }
                )
            continue

        if btype == "code":
            # 代码/算法块：内容在 code_body（不是 text）
            code = _clean_text(block.get("code_body") or block.get("text"))
            if code:
                algorithms.append(
                    {
                        "module": module,
                        "section_id": current_section["section_id"] if current_section else "",
                        "sub_type": block.get("sub_type", ""),
                        "caption": " ".join(_clean_text(c) for c in block.get("code_caption", []) or []),
                        "text": code,
                        "page": page,
                    }
                )
            continue

        if btype in ("image", "chart"):
            caption = " ".join(_clean_text(c) for c in block.get("image_caption", []) or [])
            raw_path = block.get("img_path", "")
            # 重写为可移植的相对路径 images/<module>/<basename>
            rel_path = f"images/{module}/{Path(raw_path).name}" if raw_path else ""
            figures.append(
                {
                    "module": module,
                    "section_id": current_section["section_id"] if current_section else "",
                    "caption": caption,
                    "img_path": rel_path,
                    "src_img_path": raw_path,
                    "page": page,
                }
            )
            continue

    # 回填地址
    for name, reg in registers.items():
        reg["address"] = address_map.get(name, "")

    # 给每张图关联所属章节正文（就近 prose），使纯文本模型也能理解图在讲什么。
    # 图块处理时章节正文尚未累积完整，故在此做后置填充。
    sec_text_by_id = {sid: sec.get("text", "") for sid, sec in sections.items()}
    for fig in figures:
        ctx = sec_text_by_id.get(fig.get("section_id", ""), "").strip()
        fig["context"] = ctx[:_FIGURE_CONTEXT_MAXLEN]

    return {
        "registers": list(registers.values()),
        "sections": list(sections.values()),
        "enums": enums,
        "formulas": formulas,
        "algorithms": algorithms,
        "figures": figures,
        "tables": tables,
    }


# ---------------------------------------------------------------------------
# 命令行
# ---------------------------------------------------------------------------

def _iter_content_lists(inputs: list[Path]) -> list[Path]:
    files: list[Path] = []
    for inp in inputs:
        if inp.is_dir():
            files.extend(sorted(inp.rglob("*_content_list.json")))
        elif inp.name.endswith("_content_list.json"):
            files.append(inp)
    return files


def _module_from_path(path: Path) -> str:
    """从 *_content_list.json 文件名推断模块名。"""
    stem = path.name[: -len("_content_list.json")]
    return stem.upper()


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="MinerU 输出 → knowledge.json")
    parser.add_argument("--input", nargs="+", required=True, help="MinerU 输出目录或 *_content_list.json")
    parser.add_argument("--output", required=True, help="输出 knowledge.json 路径")
    parser.add_argument("--profile", help="可选厂商适配 JSON（寄存器 regex、表头别名、水印子串）")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8")  # type: ignore[attr-defined]
        except (AttributeError, ValueError):
            pass

    args = _parse_args(argv)
    try:
        _apply_profile(Path(args.profile) if args.profile else None)
    except (OSError, json.JSONDecodeError, ValueError, re.error) as exc:
        print(f"加载 profile 失败：{exc}", file=sys.stderr)
        return 2
    files = _iter_content_lists([Path(p) for p in args.input])
    if not files:
        print("未找到任何 *_content_list.json", file=sys.stderr)
        return 1

    out = Path(args.output)
    out.parent.mkdir(parents=True, exist_ok=True)
    out_dir = out.parent
    images_root = out_dir / "images"
    markdown_root = out_dir / "markdown"

    kb: dict[str, Any] = {
        "source": [],
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
    for f in files:
        module = _module_from_path(f)
        content_list = json.loads(f.read_text(encoding="utf-8"))
        part = _convert_doc(content_list, module)

        # 步骤1：把 MinerU 的 images/ 拷进 <out>/images/<module>/，figures.img_path 已重写为该相对路径
        src_images = f.parent / "images"
        if src_images.is_dir():
            dst_images = images_root / module
            dst_images.mkdir(parents=True, exist_ok=True)
            for fig in part["figures"]:
                name = Path(fig.get("src_img_path", "")).name
                if not name:
                    continue
                src = src_images / name
                if src.exists():
                    shutil.copy2(src, dst_images / name)
            for fig in part["figures"]:
                fig.pop("src_img_path", None)

        # 步骤4：拷贝该模块 markdown 作为 prose+公式语料，记录到 documents（供后续语义/向量检索）
        src_md = f.parent / f"{module.lower()}.md"
        md_rel = ""
        if src_md.exists():
            markdown_root.mkdir(parents=True, exist_ok=True)
            md_rel = f"markdown/{module}.md"
            shutil.copy2(src_md, out_dir / md_rel)
        kb["documents"].append({"module": module, "markdown_path": md_rel})

        kb["source"].append(f"{module}.pdf")
        kb["modules"].append(module)
        for key in ("registers", "sections", "enums", "formulas", "algorithms", "figures", "tables"):
            kb[key].extend(part[key])
        print(
            f"{module}: registers={len(part['registers'])} sections={len(part['sections'])} "
            f"enums={len(part['enums'])} formulas={len(part['formulas'])} "
            f"algorithms={len(part['algorithms'])} figures={len(part['figures'])} tables={len(part['tables'])}"
        )

    out.write_text(json.dumps(kb, ensure_ascii=False, indent=2), encoding="utf-8")
    print(
        f"已写出 {out} | registers={len(kb['registers'])} sections={len(kb['sections'])} "
        f"enums={len(kb['enums'])} formulas={len(kb['formulas'])} "
        f"algorithms={len(kb['algorithms'])} figures={len(kb['figures'])} tables={len(kb['tables'])}"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
