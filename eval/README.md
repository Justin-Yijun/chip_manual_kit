# 检索评测（hit@1 / hit@5）

用于量化结构化检索、BM25、dense、RRF 和可选 rerank 的效果，避免凭主观感觉调参。

## 准备题集

仓库只提供虚构模板 `questions.template.jsonl`，不包含任何真实手册名称或真实评测问题。
复制为本地文件（`questions.local.jsonl` 已被 gitignore）：

```bash
cp eval/questions.template.jsonl eval/questions.local.jsonl
```

Bootstrap 流程：真实开发问题 → 工具初查 → **人工对照手册/代码核对** → 填 `expected` 和 `source`。
`expected` 必须是独立人工确认的锚点，不能直接照抄被测工具输出。

类型约定：
- `register`: `{"register_name", "bit_field"(可选)}`
- `figure`: `{"caption_contains"}`
- `prose`: `{"section_contains"}`

`register/figure` 题需带 `keyword`，模拟 agent 把自然语言提炼成工具关键词后的真实调用。

## 校验与运行

```bash
python eval/validate_questions.py \
  --questions eval/questions.local.jsonl --kb data/knowledge.json

python eval/run_eval.py \
  --kb data/knowledge.json --vectors data/vectors \
  --questions eval/questions.local.jsonl
```

输出分别报告：
- `search_registers` / `search_concepts` 基线；
- BM25-only；
- BM25 + dense 的 RRF；
- 可选 cross-encoder rerank。

建议每个模块至少 5 题、总量 30+；少量问题只能作为 smoke test。题集和跑分结果可能泄露
手册/项目特征，应保存在本地或受控内部仓库，不应提交到公开仓库。
