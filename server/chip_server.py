#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""芯片手册知识库 MCP Server（基于官方 FastMCP 框架）。

消费由 extract/mineru_to_kb.py 生成的 knowledge.json（MinerU 管线产物），
对 AI agent 暴露两类检索能力，指导芯片寄存器级软件开发（review / 代码编写 / 注释）：

    search_registers(query, module="")
        —— 按名称/描述/位域检索寄存器，返回地址、位域（含 access/reset/typical）
           以及所属章节解说(related_section)。

    search_concepts(query, module="")
        —— 按关键词检索"原理/公式/算法/枚举/框图"，返回章节解说、LaTeX 公式、
           伪代码算法、枚举(Value/Mnemonic/Description) 与图题，用于理解逻辑与公式。

数据文件解析顺序：环境变量 CHIP_KB_PATH > ../data/knowledge.json > 同目录 knowledge.json。

启动：python chip_server.py
"""

import argparse
import json
import logging
import os
import sys
import unicodedata
from pathlib import Path
from typing import Any

from mcp.server.fastmcp import FastMCP

# ---------------------------------------------------------------------------
# 数据加载
# ---------------------------------------------------------------------------

_MAX_RESULTS = 50

logger = logging.getLogger("chip_server")
mcp = FastMCP("chip-manual-kit")


def _resolve_data_file() -> Path:
    """按优先级定位 knowledge.json。"""
    env = os.environ.get("CHIP_KB_PATH")
    if env:
        return Path(env)
    here = Path(__file__).resolve().parent
    candidate = here.parent / "data" / "knowledge.json"
    if candidate.exists():
        return candidate
    return here / "knowledge.json"


_DATA_FILE = _resolve_data_file()


def _resolve_image(rel_path: str) -> str:
    """把 figures.img_path（相对 knowledge.json 所在目录）解析为可打开的绝对路径。"""
    if not rel_path:
        return ""
    candidate = (_DATA_FILE.parent / rel_path).resolve()
    return str(candidate) if candidate.exists() else ""


# ---------------------------------------------------------------------------
# 混合检索（BM25 + dense，RRF 融合，可选 cross-encoder 重排；缺依赖则优雅降级）
# ---------------------------------------------------------------------------

sys.path.insert(0, str(Path(__file__).resolve().parent))
from retrieval import HybridIndex  # noqa: E402


def _vectors_dir() -> Path:
    env = os.environ.get("CHIP_VECTORS_PATH")
    return Path(env) if env else (_DATA_FILE.parent / "vectors")


_SEMANTIC = HybridIndex(_vectors_dir())


def _clean_text(text: Any) -> str:
    """防御性清洗 PUA/控制字符（数据在抽取阶段已清洗，这里兜底）。"""
    if not text:
        return ""
    out: list[str] = []
    for ch in str(text):
        if "\ue000" <= ch <= "\uf8ff" or ch == "\ufffd":
            continue
        if unicodedata.category(ch).startswith("C") and ch not in "\n\t":
            continue
        out.append(ch)
    return "".join(out).strip()


def _load_kb(data_file: Path) -> dict[str, Any]:
    """加载 knowledge.json；缺失/损坏时返回空库并给出中文提示。"""
    empty = {
        "registers": [],
        "sections": [],
        "enums": [],
        "formulas": [],
        "algorithms": [],
        "figures": [],
        "tables": [],
        "documents": [],
    }
    if not data_file.exists():
        logger.warning("未找到知识库 %s，请先用 mineru_to_kb.py 生成。Server 以空库启动。", data_file)
        return empty
    try:
        raw = json.loads(data_file.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError) as exc:
        logger.warning("知识库 %s 读取失败：%s。Server 以空库启动。", data_file, exc)
        return empty
    for key in empty:
        raw.setdefault(key, [])
    return raw


_KB = _load_kb(_DATA_FILE)
_SECTION_TEXT_BY_ID: dict[str, str] = {
    sec.get("section_id", ""): sec.get("text", "")
    for sec in _KB["sections"]
    if sec.get("section_id")
}


# ---------------------------------------------------------------------------
# 检索辅助
# ---------------------------------------------------------------------------

def _module_ok(item: dict[str, Any], module_lc: str) -> bool:
    return not module_lc or module_lc in item.get("module", "").lower()


def _register_haystack(register: dict[str, Any]) -> str:
    parts = [register.get("register_name", ""), register.get("description", "")]
    for field in register.get("bit_fields", []):
        parts.append(field.get("name", ""))
        parts.append(field.get("description", ""))
    return " ".join(parts).lower()


def _related_section(register: dict[str, Any]) -> str:
    return _SECTION_TEXT_BY_ID.get(register.get("section_id", ""), "")


def _cap(items: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], str]:
    if len(items) > _MAX_RESULTS:
        note = f"命中 {len(items)} 条，仅返回前 {_MAX_RESULTS} 条，请细化 query 或 module。"
        return items[:_MAX_RESULTS], note
    return items, ""


# ---------------------------------------------------------------------------
# MCP 工具
# ---------------------------------------------------------------------------

@mcp.tool()
def search_registers(query: str, module: str = "") -> dict[str, Any]:
    """按关键词与外设模块检索芯片寄存器。

    Args:
        query: 关键词，匹配寄存器名/描述/位域名与位域描述（大小写不敏感）。
        module: 可选外设模块（如 ACME/FOO），非空时按模块过滤。

    Returns:
        {query, module, count, results:[寄存器(含 address / bit_fields[name,bit_range,
        access,reset_value,typical_value,description] / related_section 解说)], note}
    """
    query_lc = query.strip().lower()
    module_lc = module.strip().lower()
    if not _KB["registers"]:
        return {"query": query, "module": module, "count": 0, "results": [],
                "note": "知识库为空，请先用 mineru_to_kb.py 生成 knowledge.json。"}

    matched: list[dict[str, Any]] = []
    for reg in _KB["registers"]:
        if not _module_ok(reg, module_lc):
            continue
        if query_lc and query_lc not in _register_haystack(reg):
            continue
        result = dict(reg)
        result["related_section"] = _related_section(reg)
        matched.append(result)

    matched, note = _cap(matched)
    return {"query": query, "module": module, "count": len(matched), "results": matched, "note": note}


@mcp.tool()
def search_concepts(query: str, module: str = "") -> dict[str, Any]:
    """检索芯片手册中的原理/公式/算法/枚举/框图（用于理解逻辑与推导，不止寄存器）。

    Args:
        query: 关键词，匹配章节标题与正文、公式 LaTeX、算法伪代码、枚举名/助记符/描述、图题。
        module: 可选外设模块过滤。

    Returns:
        {query, module, counts, sections, formulas, algorithms, enums, figures}
        每类均为命中列表；sections 含 title/text；formulas 含 latex；enums 含 values。
    """
    q = query.strip().lower()
    m = module.strip().lower()

    def hit(text: str) -> bool:
        return not q or q in (text or "").lower()

    sections = [
        {k: s.get(k) for k in ("module", "section_id", "title", "level", "text", "page")}
        for s in _KB["sections"]
        if _module_ok(s, m) and (hit(s.get("title", "")) or hit(s.get("text", "")))
    ]
    formulas = [
        f for f in _KB["formulas"]
        if _module_ok(f, m) and hit(f.get("latex", ""))
    ]
    algorithms = [
        a for a in _KB["algorithms"]
        if _module_ok(a, m) and hit(a.get("text", ""))
    ]
    enums = []
    for e in _KB["enums"]:
        if not _module_ok(e, m):
            continue
        values = e.get("values", [])
        if hit(e.get("name", "")) or any(
            hit(v.get("mnemonic", "")) or hit(v.get("description", "")) for v in values
        ):
            enums.append(e)
    tables = []
    for t in _KB["tables"]:
        if not _module_ok(t, m):
            continue
        flat = " ".join(" ".join(r) for r in t.get("rows", [])) + " " + " ".join(t.get("header", []))
        if hit(t.get("caption", "")) or hit(flat):
            tables.append(t)
    figures = []
    for fig in _KB["figures"]:
        if not _module_ok(fig, m) or not (hit(fig.get("caption", "")) or hit(fig.get("context", ""))):
            continue
        item = dict(fig)
        item["image_abs_path"] = _resolve_image(fig.get("img_path", ""))
        figures.append(item)

    sections, _ = _cap(sections)
    tables, _ = _cap(tables)
    return {
        "query": query,
        "module": module,
        "counts": {
            "sections": len(sections),
            "formulas": len(formulas),
            "algorithms": len(algorithms),
            "enums": len(enums),
            "tables": len(tables),
            "figures": len(figures),
        },
        "sections": sections,
        "formulas": formulas,
        "algorithms": algorithms,
        "enums": enums,
        "tables": tables,
        "figures": figures,
    }


@mcp.tool()
def search_prose(query: str, module: str = "", top_k: int = 5, rerank: bool = True) -> dict[str, Any]:
    """混合检索芯片手册（BM25 词法 + dense 向量，可选融合，可选 cross-encoder 重排）。

    适合"既含标识符又含概念"的自然语言查询（如 "how does DROP_CNT saturation work"）。
    融合与重排全在服务端完成，**默认只返回 top 5 条精炼结果**，避免把几十条粗召回塞进上下文、
    稀释注意力。与 search_registers（精确寄存器/位域）、search_concepts（关键词）互补。

    融合策略由环境变量 CHIP_FUSION 控制：rrf（默认）或 relative_score；
    relative_score 权重见 CHIP_FUSION_DENSE_WEIGHT / CHIP_FUSION_BM25_WEIGHT。
    可选 top-rank bonus（默认关闭）：CHIP_FUSION_BONUS_RANK1 / CHIP_FUSION_BONUS_RANK2_3，
    防止某一路精确命中（如标识符）在融合时被"两路都还行"的候选稀释掉。

    Args:
        query: 自然语言查询（英文优先，手册为英文）。
        module: 可选外设模块过滤（如 ACME/FOO）。
        top_k: 返回条数（默认 5；建议 5–8，避免上下文膨胀）。
        rerank: 是否用本地 cross-encoder 精排（需设 CHIP_RERANK_MODEL；缺失则自动跳过）。

    Returns:
        {query, module, count, results:[{module, kind, title, breadcrumb, page, section_id,
        text, fusion_score, rrf_score, dense_score, bm25_score}], note}
        note 标明实际用的方法（bm25 / rrf(...) / relative_score(...) / +rerank）与降级情况。
    """
    results, err = _SEMANTIC.search(
        query, module, max(1, min(top_k, _MAX_RESULTS)), rerank=rerank)
    return {"query": query, "module": module, "count": len(results), "results": results, "note": err}


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    """解析启动参数。默认 stdio（形态 A，本地按需拉起）；--http 走 streamable-http（形态 B，常驻服务）。

    参数优先级：命令行 > 环境变量 > 默认。环境变量便于用 mcp.json 的 env 注入：
        CHIP_MCP_TRANSPORT = stdio | streamable-http | sse
        CHIP_MCP_HOST      = 监听地址（默认 127.0.0.1）
        CHIP_MCP_PORT      = 监听端口（默认 8000）
    """
    env_transport = os.environ.get("CHIP_MCP_TRANSPORT", "stdio")
    env_host = os.environ.get("CHIP_MCP_HOST", "127.0.0.1")
    env_port = int(os.environ.get("CHIP_MCP_PORT", "8000"))

    p = argparse.ArgumentParser(description="芯片手册知识库 MCP Server（默认 stdio；--http 常驻服务）。")
    p.add_argument("--http", action="store_true",
                   help="以 HTTP(streamable-http) 常驻服务运行（形态 B），供远程/多客户端连接。")
    p.add_argument("--transport", choices=("stdio", "streamable-http", "sse"), default=None,
                   help="显式指定传输方式（覆盖 --http 与环境变量）。")
    p.add_argument("--host", default=env_host, help="HTTP 监听地址（默认 127.0.0.1；对外服务用 0.0.0.0）。")
    p.add_argument("--port", type=int, default=env_port, help="HTTP 监听端口（默认 8000）。")
    args = p.parse_args(argv)

    if args.transport is None:
        args.transport = "streamable-http" if args.http else env_transport
    return args


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s", stream=sys.stderr)
    args = _parse_args()
    logger.info(
        "已加载知识库 %s：registers=%d sections=%d enums=%d formulas=%d algorithms=%d figures=%d tables=%d",
        _DATA_FILE,
        len(_KB["registers"]), len(_KB["sections"]), len(_KB["enums"]),
        len(_KB["formulas"]), len(_KB["algorithms"]), len(_KB["figures"]), len(_KB["tables"]),
    )
    if args.transport == "stdio":
        mcp.run()  # 形态 A：本地 stdio，由客户端按需拉起
    else:
        # 形态 B：常驻 HTTP 服务。host/port 属于 FastMCP 的构造期 settings，运行前覆盖即可。
        mcp.settings.host = args.host
        mcp.settings.port = args.port
        logger.info("以 %s 常驻服务启动：http://%s:%d%s",
                    args.transport, args.host, args.port, mcp.settings.streamable_http_path)
        mcp.run(transport=args.transport)
