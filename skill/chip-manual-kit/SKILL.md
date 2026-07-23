---
name: chip_manual_kit
description: >
  从芯片手册/数据手册 PDF 结构化提取寄存器、位域(bit field)、地址、枚举(enum)、
  公式、算法与章节解说，生成 AI agent 可检索的知识库(knowledge.json)，指导寄存器级
  vibe coding（review / 写驱动 / 加注释）。USE WHEN: 用户询问某芯片寄存器地址/位域/
  复位值/访问类型、错误码枚举(如 *_ERROR_E)、外设模块(如 ACME/FOO)、要
  "读芯片手册/datasheet"、"把手册转成 MCP / 知识库 JSON"、"查寄存器 XXX 的 bit 定义"、
  "这个 error code 是什么意思"。两种消费方式：(1) 禁用 MCP 的环境用 query_kb.py 终端查询；
  (2) 支持 MCP 的环境用 server/chip_server.py 作为 MCP server。
---

# Chip Manual → Programming Knowledge (portable)

把芯片手册 PDF 变成 agent 可检索的编程知识库。管线三段：**PDF → knowledge.json → 查询**。
本 skill 自包含、可移植（Cursor / Claude Code / Copilot / MiMo 均可用）。

## 何时激活

按**意图**匹配，而非精确措辞：

- 中文：「查一下 ACME 里和 jobid 相关的寄存器」「CTRL_STATUS 每个 bit 是什么」「这个 error code 0x20 是什么」「数据压缩位宽怎么算」「把这份芯片手册转成知识库」
- English: "what's the address of ACME CTRL_STATUS", "bit fields with reset values", "what does error code 0x20 mean", "parse this datasheet into a knowledge base"

## 数据 schema（knowledge.json）

```jsonc
{
  "modules": ["ACME", "FOO"],
  "registers": [{
    "module": "ACME",
    "register_name": "ACME_CTRL_STATUS",
    "address": "0x87E040400038",
    "section_id": "acme:...",              // 显式外键 → sections
    "bit_fields": [{"name","bit_range","access","reset_value","typical_value","description"}],
    "page": 47
  }],
  "sections":  [{"section_id","module","title","level","text","page"}],
  "enums":     [{"module","name","section_id","values":[{"value","mnemonic","description"}],"page"}],
  "formulas":  [{"module","section_id","latex","page"}],
  "algorithms":[{"module","section_id","sub_type","caption","text","page"}],  // 伪代码/算法(来自 code_body)
  "tables":    [{"module","section_id","caption","header":[...],"rows":[[...]],"page"}], // 位布局/编码/包格式等结构化表
  "figures":   [{"module","section_id","caption","img_path","context","page"}],  // img_path 相对 data/，真实图在 data/images/<module>/；context=所属章节就近正文(供纯文本模型理解框图)
  "documents": [{"module","markdown_path"}]   // 每模块完整 markdown(prose+公式语料，供语义/向量检索)
}
```

> knowledge.json 是"结构化 + 回源引用"层，不是全量：图只存路径(真实 .jpg 在 `data/images/`)，
> 每模块完整 markdown 在 `data/markdown/`，需要原文/原图时按 `page` / `img_path` 回源。

## 消费（按目标端二选一）

**A. 禁用 MCP（如受策略限制的 VS Code Copilot）→ 用 CLI 查询**
```bash
# 查寄存器（含 access/reset/typical 与所属章节解说）
python query_kb.py --data knowledge.json --query ctrl_status --module ACME
# 查原理/公式/算法/枚举/图（理解逻辑、解释 error code）
python query_kb.py --data knowledge.json --mode concepts --query "receive error" --module ACME
python query_kb.py --data knowledge.json --mode concepts --query "sequence id" --module ACME
```
零第三方依赖，Python 3.9+ 即可。

**B. 支持 MCP（Cursor / Claude Code）→ 作为 MCP server**
见仓库根 `README.md` 与 `examples/mcp.json.template`：把 `server/chip_server.py` 注册为
MCP server（Windows `command` 指向 venv 的 `python.exe`），暴露三个互补工具：
- `search_registers(query, module)` — 精确寄存器/位域/地址
- `search_concepts(query, module)` — 关键词命中原理/公式/算法/枚举/表/图
- `search_prose(query, module, top_k=5, rerank=True)` — 混合检索（BM25 + dense，默认 RRF；
  可设 `CHIP_FUSION=relative_score` 做 A/B；可选 cross-encoder 精排）。融合/重排在服务端完成，
  **默认只返回 top 5**，避免上下文膨胀。

## 换手册 / 重建知识库（一键复现）

1. 用 MinerU 解析 PDF（CPU）：`mineru -p manual.pdf -o out -b pipeline`
2. 一键构建（knowledge.json + 混合检索索引），**只要手头有原始 PDF 就带上 `--verify-with-docling`**：
   ```bash
   python scripts/build_kb.py --mineru-out out --embed-model /path/to/bge-small-en-v1.5 \
     --verify-with-docling MODULE.pdf
   ```
   仅重建 JSON、跳过向量：加 `--skip-vectors`（混合检索则只有 BM25）。
3. 覆盖 `data/` 即可，MCP/CLI 无需改动。索引参数写入 `data/vectors/meta.json`，任意工具可复现。

## 注意事项
- 数据经 PUA/水印清洗；MinerU OCR 仍可能有个别误识（如 0↔O），关键地址/位宽建议回查原 PDF 页码(page)。
- **默认用 MinerU 建主库，但每次建库都应该加 `--verify-with-docling MODULE.pdf` 做交叉校验**
  （不装 docling 会自动跳过，不影响主库）：会自动打印 + 落盘 `data/docling_compare_MODULE.json`，
  只用来发现 MinerU 可能漏挂/错抽的寄存器，**不会自动合并/覆盖主库**。看到 `only_docling` 较多时，
  回原书人工确认后再修 profile/抽取规则、重建主库。不要因为"没装 docling"或"忘了这一步"就把
  未经交叉校验的库当成最终结果发布。详见仓库 `docs/USER_GUIDE.md` §3。
- 手册增量/换版：替换对应模块的 MinerU 输出后整库重建 `knowledge.json` + vectors，并用旧库/Docling 做差分验收（同 USER_GUIDE §4）。
- `knowledge.json` 为手册派生产物，是否入库取决于手册版权，默认建议 `.gitignore`。
