# 适配其它厂商芯片手册：抽取与测试方法

目标不是“整本 PDF 跑完才发现 0 个寄存器”，而是先用一个**代表性小样（golden slice）**
识别文档特征、配置 profile、建立人工 gold 问题，再扩大到整本/整批手册。

## 0. 哪些是通用的，哪些需要适配

通用层：
- MinerU 的阅读序、标题、表格、图片、公式抽取（默认后端）；
- 可选 Docling 抽取后端（`extract/docling_to_kb.py`），产出同一 content_list 形状；
- `knowledge.json` schema；
- 结构感知分块、BM25 + dense + RRF（默认）/ relative_score（可选 A/B）、可选 rerank；
- MCP stdio/HTTP、评测 hit@1/hit@5。

厂商适配层：
- 寄存器命名规则；
- 位域/地址汇总/枚举表的列名；
- 地址格式；
- 水印/页眉噪声；
- 文档语言（英文 bge-small vs 多语言模型）。

## 1. 选择 golden slice

先选 10–30 页，必须同时包含：
1. 寄存器地址汇总表；
2. 至少 2 个寄存器位域表（含 access/reset/description）；
3. 枚举/编码表；
4. 一段原理正文；
5. 一张框图；
6. 如文档有公式/伪代码，也放入样本。

这比随机截前 20 页更重要：目录/概述页无法验证寄存器抽取。

## 2. MinerU 解析小样并审计原始特征

```bash
mineru -p golden-slice.pdf -o out_golden -b pipeline
python extract/audit_source.py --input out_golden --json audit-before.json
```

重点看：
- `register_anchor_count` 是否 > 0；
- `table_types.bitfield/summary/enum` 是否符合人工数数；
- `unknown_table_headers` 里是否出现该厂商的 `Bits / Field / Offset / Default / Meaning`；
- 标题层级、image/equation/code block 是否存在；
- 是否警告同一模块有多个 content_list（不要同时摄入分段和合并产物）。

## 3. 创建 extraction profile（不改代码即可适配常见差异）

复制 `examples/extraction-profile.json.template`：

```bash
cp examples/extraction-profile.json.template my-vendor-profile.json
```

可配置：
- `register_name_pattern`：寄存器 token 的 Python regex；
- `register_require_underscore`：是否强制寄存器名含 `_`；
- `bit_position_patterns`：追加 `[7:0]`、`15:0` 等位域位置 regex；
- `enum_suffixes`：替换默认 `_E/_S` 枚举类型后缀；
- `noise_substrings`：额外水印/版权噪声；
- `table_header_aliases`：把厂商列名映射到统一语义：
  `bit_position / field_name / register / address / mnemonic / value /
  access / reset / typical / description`。

再次审计，确认 profile 确实改善分类：

```bash
python extract/audit_source.py --input out_golden \
  --profile my-vendor-profile.json --json audit-after.json
```

原则：**先看实际表头，再加别名；不要凭感觉写正则。**
若设置 `register_require_underscore=false`，务必同时用 `register_name_pattern` 限定厂商前缀/
命名形状；否则正文里的全大写模块词（如 `ACME`、`STATUS`）可能被误认成寄存器锚点。

## 4. 构建并验证知识库

```bash
python extract/mineru_to_kb.py --input out_golden \
  --profile my-vendor-profile.json --output data/knowledge.json

python extract/validate_kb.py --kb data/knowledge.json
# CI 或希望所有覆盖率告警都失败：
python extract/validate_kb.py --kb data/knowledge.json --strict
```

验证器检查：
- schema 数组类型；
- 重复 section_id、孤立外键；
- 重复寄存器；
- 图片路径；
- 寄存器地址/位域覆盖率；
- section 正文和 figure context 覆盖率。

然后**人工抽查**：
- 随机 10 个寄存器：名称、地址、位域、access、reset；
- 3 张表、3 个枚举、3 张图；
- 页码能否回源；
- 关键数字是否有 OCR 的 `0/O`、`1/l` 错误。

`validate_kb.py` 能发现结构/覆盖问题，不能证明每个数值都正确；数字仍需人工 gold。

## 5. 建立该厂商自己的检索 gold

复制模板：

```bash
cp eval/questions.template.jsonl eval/my-vendor-questions.jsonl
```

问题来源优先级：
1. 真实 debug/开发问题；
2. 驱动代码里的寄存器常量；
3. 手册中的典型地址、位域、枚举和原理章节。

每条必须人工对照手册，填写 `source`。`expected` 不是被测工具的输出：

```json
{"id":"foo-01","query":"what is CONTROL address","keyword":"CONTROL","module":"FOO","type":"register","expected":{"register_name":"CONTROL","bit_field":"ENABLE"},"source":"FOO manual p123"}
```

校验题集 schema 和锚点：

```bash
python eval/validate_questions.py \
  --questions eval/my-vendor-questions.jsonl --kb data/knowledge.json
```

## 6. 构建向量并跑检索评测

```bash
python extract/vectorize.py --kb data/knowledge.json --out data/vectors \
  --model /path/to/embed-model

python eval/run_eval.py \
  --questions eval/my-vendor-questions.jsonl \
  --kb data/knowledge.json --vectors data/vectors
```

建议最小门槛（按业务调整）：
- register/figure：hit@5 ≥ 95%；
- prose hybrid：hit@5 ≥ 85%；
- 任何 safety/复位/地址关键题：必须 hit@1 或单独设硬门槛；
- BM25、hybrid_rrf、hybrid_relative、hybrid_rerank 分开记录，避免换算法后悄悄回退。
- 融合 A/B：默认 `CHIP_FUSION=rrf`；对照可设 `CHIP_FUSION=relative_score`
  （权重 `CHIP_FUSION_DENSE_WEIGHT` / `CHIP_FUSION_BM25_WEIGHT`，默认各 0.5）。
- Top-rank bonus A/B（默认关闭，`eval/run_eval.py` 里的 `hybrid_rrf_bonus` 配置）：
  `CHIP_FUSION_BONUS_RANK1` / `CHIP_FUSION_BONUS_RANK2_3`，给"任一路排第 1/第 2-3"
  的候选加固定分，防止精确命中（如标识符）被"两路都还行"的候选融合稀释。确认
  在你的 gold 题上确实提升 hit@1/hit@5 后才建议设为生产默认值。

题目少时结果只算 smoke test；每个模块至少 5 题、总量 30+ 才适合比较算法。

## 6b. Docling 校验（默认仍是 MinerU）

**准确度优先时的标准用法**：MinerU 建主库，Docling 只做对照，**不自动覆盖** `data/knowledge.json`。  
完整说明（含手册增量/换版）见 **[`USER_GUIDE.md`](USER_GUIDE.md)**。

同一 golden-slice（或整本）可再跑一遍 Docling：

```bash
pip install docling

python extract/docling_to_kb.py --pdf golden-slice.pdf --module ACME \
  --out-dir out_docling --output data/kb_docling.json \
  --profile my-vendor-profile.json

python extract/compare_backends.py \
  --a data/knowledge.json --b data/kb_docling.json \
  --label-a mineru --label-b docling --json compare-report.json
```

判定要点：`only_docling` 多 → 疑似 MinerU 漏挂（回原书后再改主库）；地址不一致 → 禁止静默采信任一侧；  
位域 Jaccard 低 → 调 profile。新厂商落地仍走本章 §1–§6；**换手册版本 / 加模块**见 USER_GUIDE §4。

不整库迁移到 LlamaIndex：只借鉴 relative_score 融合，检索与 MCP 仍用本仓库实现。

## 7. 全量扩展与回归

小样通过后再跑整本。全量构建完成后：
1. 再跑 `validate_kb.py`；
2. 跑相同 gold，不修改 expected；
3. 检查模块间同名寄存器、重复分段产物；
4. 保存 `audit JSON`、`vectors/meta.json`、评测输出，作为可复现记录。

## 8. 何时 profile 不够、必须改 parser

以下情况需要新增解析器逻辑并补 fixture 单测：
- 地址不是十六进制，而是 base + offset/多 BAR 公式；
- 位域范围不是 `<63:0>`，而是 `[63:0]`、`63–0` 或拆成 MSB/LSB 两列；
- 一个表跨页且每页重复表头；
- 寄存器名/描述在合并单元格中；
- 枚举嵌在位域描述而非独立表；
- 多语言表头或扫描质量导致列错位。

提交新适配时，仿照 `tests/fixtures/acme_content_list.json`：
- 用**最小、脱敏、厂商中立** fixture 重现格式；
- 在 `tests/test_extraction_adaptation.py` 增加断言；
- 不提交受版权保护的原始 PDF 或完整派生知识库。

## 9. 一条命令跑核心回归

```bash
python -m unittest discover -s tests -p "test_*.py" -v
python -m unittest eval.test_retrieval -v
python eval/validate_questions.py --questions eval/questions.jsonl --kb data/knowledge.json
python extract/validate_kb.py --kb data/knowledge.json
python eval/run_eval.py
```

这套流程把“能跑”拆成四个可验证层次：
**源格式识别 → 结构化抽取 → schema/覆盖率 → 真实问题检索质量**。
