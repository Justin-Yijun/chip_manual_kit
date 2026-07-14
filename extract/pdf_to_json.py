#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""芯片手册 PDF 结构化切片工具。

将常见英文芯片手册 PDF 解析为结构化 JSON，供 MCP Server 检索使用。
处理三类信息：
    1. 寄存器汇总表（Register / BAR / Address）—— 提供寄存器地址。
    2. 位域详情表（Bit Pos / Field Name / Field Description）—— 提供位域列表。
    3. 叙述性解说正文 —— 按章节标题切片为 sections，并将同章节标题
       冗余进对应寄存器的 description（方案 C + B 轻量版）。

手册中带有红色对角水印（Helvetica-Bold），会污染文本提取，脚本在解析前
先将其过滤掉。

用法示例：
    python pdf_to_json.py --input doc/acme/ACME.pdf --output registers.json
    python pdf_to_json.py --input doc/acme --output registers.json
"""

import argparse
import json
import logging
import re
import sys
import unicodedata
from pathlib import Path
from typing import Any

import pdfplumber

# ---------------------------------------------------------------------------
# 常量定义
# ---------------------------------------------------------------------------

# 水印字符特征：红色（RGB 1,0,0）且字体为 Helvetica-Bold
_WATERMARK_COLOR = (1.0, 0.0, 0.0)
_WATERMARK_FONT_SUFFIX = "Helvetica-Bold"

# 寄存器名：全大写字母/数字/下划线，允许 (0..2) 形式的实例范围
_RE_REGISTER_NAME = re.compile(r"^[A-Z][A-Z0-9_]*(?:\([0-9.]+\)[A-Z0-9_]*)*$")

# 章节标题：形如 "ACME Control Registers"（含空格且以 Registers 结尾）
_RE_SECTION_HEADING = re.compile(r"^[A-Z][A-Za-z0-9 /()-]+Registers?$")

# 位域位置：形如 <63:48> 或 <0>
_RE_BIT_POS = re.compile(r"^<\d+(?::\d+)?>$")

# 页眉/页脚版权样板行关键字，需从解说正文中剔除
_FOOTER_KEYS = (
    "document classification",
    "copyright",
    "proprietary",
    "confidential",
)

# 十六进制地址：0x 开头
_RE_HEX = re.compile(r"0x[0-9A-Fa-f]+")

# 位域详情表表头关键字
_BITFIELD_HEADER_KEYS = ("bit pos", "field name")

# 寄存器汇总表表头关键字
_SUMMARY_REQUIRED_KEY = "register"
_SUMMARY_ADDRESS_KEYS = ("address", "bar")

logger = logging.getLogger("pdf_to_json")


# ---------------------------------------------------------------------------
# 通用辅助函数
# ---------------------------------------------------------------------------

def _keep_char(obj: dict[str, Any]) -> bool:
    """过滤器：丢弃红色 Helvetica-Bold 水印字符，保留其余对象。"""
    if obj.get("object_type") != "char":
        return True
    is_watermark_font = str(obj.get("fontname", "")).endswith(_WATERMARK_FONT_SUFFIX)
    is_watermark_color = obj.get("non_stroking_color") == _WATERMARK_COLOR
    return not (is_watermark_font and is_watermark_color)


def _clean_text(text: str | None) -> str:
    """剔除 PDF 符号字体残留的私有区(PUA)字形、替换符与控制字符。

    某些手册用符号字体承载 reserved 标记等图元，抽取后会落到 Unicode
    私有区（U+E000–U+F8FF），污染字段名并在 GBK 控制台触发 UnicodeEncodeError。
    """
    if not text:
        return ""
    cleaned: list[str] = []
    for ch in text:
        if "\ue000" <= ch <= "\uf8ff":  # 私有使用区
            continue
        if ch == "\ufffd":  # 替换字符（乱码占位）
            continue
        category = unicodedata.category(ch)
        if category.startswith("C") and ch not in "\n\t":  # 其它控制字符
            continue
        cleaned.append(ch)
    return "".join(cleaned)


def _norm(text: str | None) -> str:
    """归一化文本：清洗脏字、合并空白、去除首尾空格。"""
    if not text:
        return ""
    return re.sub(r"\s+", " ", _clean_text(text)).strip()


def _section_id(module: str, title: str) -> str:
    """由模块名与章节标题派生稳定的显式外键 id。"""
    if not title:
        return ""
    slug = re.sub(r"[^a-z0-9]+", "-", title.lower()).strip("-")
    return f"{module.lower()}:{slug}"


def _is_register_name(line: str) -> bool:
    """判断一行文本是否是寄存器名标题。"""
    if len(line) < 4 or "_" not in line:
        return False
    return bool(_RE_REGISTER_NAME.match(line))


def _is_section_heading(line: str) -> bool:
    """判断一行文本是否是章节标题（且不是寄存器名）。"""
    if _is_register_name(line):
        return False
    return bool(_RE_SECTION_HEADING.match(line)) and " " in line


def _extract_hex(text: str) -> str:
    """从单元格文本中提取首个十六进制地址；找不到时返回归一化原文。"""
    match = _RE_HEX.search(text or "")
    return match.group(0) if match else _norm(text)


def _header_cells(table: list[list[str | None]]) -> list[str]:
    """取表格首行并做小写归一化，用于表头识别。"""
    if not table:
        return []
    return [_norm(cell).lower() for cell in table[0]]


def _is_bitfield_table(table: list[list[str | None]]) -> bool:
    """判断是否为位域详情表。"""
    header = " ".join(_header_cells(table))
    return all(key in header for key in _BITFIELD_HEADER_KEYS)


def _is_summary_table(table: list[list[str | None]]) -> bool:
    """判断是否为寄存器汇总表（含 Register 与 Address/BAR 列）。"""
    cells = _header_cells(table)
    header = " ".join(cells)
    if _SUMMARY_REQUIRED_KEY not in header:
        return False
    if any(key in header for key in _BITFIELD_HEADER_KEYS):
        return False
    return any(key in header for key in _SUMMARY_ADDRESS_KEYS)


def _find_column(cells: list[str], *keys: str) -> int:
    """在表头单元格中查找首个包含指定关键字的列索引，找不到返回 -1。"""
    for idx, cell in enumerate(cells):
        if any(key in cell for key in keys):
            return idx
    return -1


# ---------------------------------------------------------------------------
# 表格解析
# ---------------------------------------------------------------------------

def _parse_bitfields(table: list[list[str | None]]) -> list[dict[str, str]]:
    """从位域详情表解析出位域列表 [{name, bit_range, description}]。"""
    cells = _header_cells(table)
    idx_bit = _find_column(cells, "bit pos")
    idx_name = _find_column(cells, "field name")
    idx_desc = _find_column(cells, "field description", "description")

    bit_fields: list[dict[str, str]] = []
    for row in table[1:]:
        bit_range = _norm(row[idx_bit]) if idx_bit >= 0 and idx_bit < len(row) else ""
        if not _RE_BIT_POS.match(bit_range):
            # 跳过表头重复行或跨页残行等噪声
            continue
        name = _norm(row[idx_name]) if 0 <= idx_name < len(row) else ""
        desc = _norm(row[idx_desc]) if 0 <= idx_desc < len(row) else ""
        bit_fields.append(
            {
                "name": name or "RESERVED",
                "bit_range": bit_range,
                "description": desc,
            }
        )
    return bit_fields


def _parse_summary(table: list[list[str | None]]) -> dict[str, str]:
    """从寄存器汇总表解析 {寄存器名: 地址} 映射。"""
    cells = _header_cells(table)
    idx_name = _find_column(cells, "register")
    # 地址列优先匹配 "address"，找不到再回退到 "bar"
    idx_addr = _find_column(cells, "address")
    if idx_addr < 0:
        idx_addr = _find_column(cells, "bar")
    mapping: dict[str, str] = {}
    for row in table[1:]:
        if idx_name < 0 or idx_name >= len(row):
            continue
        # 汇总表单元格可能混入水印残字，取首个符合寄存器名规则的 token
        raw_name = _norm(row[idx_name])
        name = next((tok for tok in raw_name.split() if _is_register_name(tok)), "")
        if not name:
            continue
        addr = _extract_hex(row[idx_addr]) if 0 <= idx_addr < len(row) else ""
        mapping[name] = addr
    return mapping


# ---------------------------------------------------------------------------
# 单页解析
# ---------------------------------------------------------------------------

def _line_above(lines: list[dict[str, Any]], top: float, predicate) -> str:
    """在给定纵坐标之上，返回最接近的满足 predicate 的文本行内容。"""
    best_text = ""
    best_top = -1.0
    for line in lines:
        line_top = line["top"]
        text = _norm(line["text"])
        if line_top < top and predicate(text) and line_top > best_top:
            best_top = line_top
            best_text = text
    return best_text


def _collect_sections(
    lines: list[dict[str, Any]],
    table_bboxes: list[tuple[float, float, float, float]],
    module: str,
    page_number: int,
    current_section: dict[str, Any] | None,
    sections: list[dict[str, Any]],
) -> dict[str, Any] | None:
    """收集叙述性解说正文，按章节标题切片写入 sections。

    返回更新后的“当前章节”累加器（可能跨页续接）。
    """

    def _inside_table(line_top: float) -> bool:
        return any(bbox[1] <= line_top <= bbox[3] for bbox in table_bboxes)

    for line in lines:
        text = _norm(line["text"])
        if not text:
            continue
        if _is_section_heading(text):
            # 遇到新章节标题，先落盘上一章节
            if current_section and current_section["text"]:
                sections.append(current_section)
            current_section = {
                "module": module,
                "title": text,
                "text": "",
                "page": page_number,
                "section_id": _section_id(module, text),
            }
            continue
        if current_section is None:
            continue
        # 过滤表格内容、寄存器名标题与地址跳转提示，仅保留自由文本
        if _inside_table(line["top"]):
            continue
        if _is_register_name(text) or text.startswith("<"):
            continue
        if text.startswith("See Table") or text.startswith("See page"):
            continue
        if any(key in text.lower() for key in _FOOTER_KEYS):
            continue
        # 跳过页眉运行标题（单词 "Registers"）
        if " " not in text and text.lower().rstrip("s") == "register":
            continue
        current_section["text"] = _norm(f"{current_section['text']} {text}")
    return current_section


def _parse_pdf(path: Path) -> dict[str, Any]:
    """解析单个 PDF，返回 {registers, sections}。"""
    module = path.stem.upper()
    registers: dict[str, dict[str, Any]] = {}
    sections: list[dict[str, Any]] = []
    address_map: dict[str, str] = {}

    last_register_name = ""
    current_section: dict[str, Any] | None = None

    with pdfplumber.open(str(path)) as pdf:
        for page in pdf.pages:
            clean = page.filter(_keep_char)
            page_number = page.page_number  # 1-based
            lines = clean.extract_text_lines()

            found_tables = clean.find_tables()
            table_bboxes = [tbl.bbox for tbl in found_tables]

            for tbl in found_tables:
                data = tbl.extract()
                if _is_summary_table(data):
                    address_map.update(_parse_summary(data))
                    continue
                if not _is_bitfield_table(data):
                    continue

                # 位域表：向上寻找最近的寄存器名标题；跨页续表时回退到上一寄存器
                reg_name = _line_above(lines, tbl.bbox[1], _is_register_name)
                if not reg_name:
                    reg_name = last_register_name
                if not reg_name:
                    continue
                last_register_name = reg_name

                section_title = _line_above(lines, tbl.bbox[1], _is_section_heading)
                bit_fields = _parse_bitfields(data)

                if reg_name in registers:
                    # 跨页续接，追加位域
                    registers[reg_name]["bit_fields"].extend(bit_fields)
                else:
                    registers[reg_name] = {
                        "module": module,
                        "register_name": reg_name,
                        "address": "",
                        "description": section_title,
                        "section_id": _section_id(module, section_title),
                        "bit_fields": bit_fields,
                        "page": page_number,
                    }

            current_section = _collect_sections(
                lines, table_bboxes, module, page_number, current_section, sections
            )

    # 收尾：落盘最后一个章节
    if current_section and current_section["text"]:
        sections.append(current_section)

    # 回填地址
    for name, reg in registers.items():
        reg["address"] = address_map.get(name, "")

    return {"registers": list(registers.values()), "sections": sections}


# ---------------------------------------------------------------------------
# 命令行入口
# ---------------------------------------------------------------------------

def _iter_pdf_files(input_path: Path) -> list[Path]:
    """根据输入路径返回待解析的 PDF 文件列表。"""
    if input_path.is_dir():
        return sorted(input_path.glob("*.pdf"))
    return [input_path]


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    """解析命令行参数。"""
    parser = argparse.ArgumentParser(
        description="将芯片手册 PDF 结构化切片为寄存器 JSON。",
    )
    parser.add_argument(
        "--input",
        required=True,
        help="输入 PDF 文件或包含多个 PDF 的目录。",
    )
    parser.add_argument(
        "--output",
        required=True,
        help="输出 JSON 文件路径。",
    )
    parser.add_argument(
        "--verbose",
        action="store_true",
        help="打印详细解析日志。",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    """程序主入口，成功返回 0，失败返回非零。"""
    args = _parse_args(argv)
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(levelname)s: %(message)s",
    )

    input_path = Path(args.input)
    if not input_path.exists():
        logger.error("输入路径不存在：%s", input_path)
        return 1

    pdf_files = _iter_pdf_files(input_path)
    if not pdf_files:
        logger.error("未在输入路径中找到任何 PDF 文件：%s", input_path)
        return 1

    all_registers: list[dict[str, Any]] = []
    all_sections: list[dict[str, Any]] = []
    for pdf_file in pdf_files:
        if pdf_file.suffix.lower() != ".pdf":
            logger.warning("跳过非 PDF 文件：%s", pdf_file)
            continue
        logger.info("正在解析：%s", pdf_file.name)
        try:
            parsed = _parse_pdf(pdf_file)
        except Exception as exc:  # noqa: BLE001 - 单个文件失败不应中断整体
            logger.error("解析失败 %s：%s", pdf_file.name, exc)
            continue
        logger.info(
            "  → 寄存器 %d 个，解说章节 %d 段",
            len(parsed["registers"]),
            len(parsed["sections"]),
        )
        all_registers.extend(parsed["registers"])
        all_sections.extend(parsed["sections"])

    if not all_registers:
        logger.warning("未解析出任何寄存器，请检查 PDF 格式是否匹配。")

    result = {
        "source": [f.name for f in pdf_files],
        "registers": all_registers,
        "sections": all_sections,
    }

    output_path = Path(args.output)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(
        json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    logger.info(
        "已写出 %s（寄存器 %d，章节 %d）",
        output_path,
        len(all_registers),
        len(all_sections),
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
