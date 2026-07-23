#!/usr/bin/env python3
"""厂商中立 fixture：验证 profile 表头适配、结构化抽取和 KB 审计。"""

import json
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
EXTRACT = ROOT / "extract"
FIXTURES = Path(__file__).resolve().parent / "fixtures"
sys.path.insert(0, str(EXTRACT))

import mineru_to_kb as conv  # noqa: E402
from audit_source import audit  # noqa: E402
from validate_kb import validate  # noqa: E402

# `_apply_profile` 会原地改写模块级全局正则/开关（供 CLI 单次 --profile 用），
# 未提供 --profile 时不会自动还原，因此这里在导入时快拍默认值，供各测试类在
# setUp/tearDown 里还原，避免类之间因执行顺序互相污染全局状态。
_DEFAULT_RE_REGISTER_NAME = conv._RE_REGISTER_NAME
_DEFAULT_REGISTER_REQUIRE_UNDERSCORE = conv._REGISTER_REQUIRE_UNDERSCORE
_DEFAULT_ENUM_SUFFIXES = conv._ENUM_SUFFIXES
_DEFAULT_NOISE_SUBSTRINGS = conv._NOISE_SUBSTRINGS
_DEFAULT_HEADER_ALIASES = {k: list(v) for k, v in conv._HEADER_ALIASES.items()}
_DEFAULT_BIT_POSITION_PATTERNS = list(conv._BIT_POSITION_PATTERNS)


def _restore_default_profile() -> None:
    conv._RE_REGISTER_NAME = _DEFAULT_RE_REGISTER_NAME
    conv._REGISTER_REQUIRE_UNDERSCORE = _DEFAULT_REGISTER_REQUIRE_UNDERSCORE
    conv._ENUM_SUFFIXES = _DEFAULT_ENUM_SUFFIXES
    conv._NOISE_SUBSTRINGS = _DEFAULT_NOISE_SUBSTRINGS
    conv._HEADER_ALIASES.clear()
    conv._HEADER_ALIASES.update({k: list(v) for k, v in _DEFAULT_HEADER_ALIASES.items()})
    conv._BIT_POSITION_PATTERNS[:] = list(_DEFAULT_BIT_POSITION_PATTERNS)


class ExtractionAdaptationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        conv._apply_profile(FIXTURES / "acme_profile.json")
        cls.blocks = json.loads(
            (FIXTURES / "acme_content_list.json").read_text(encoding="utf-8")
        )
        cls.part = conv._convert_doc(cls.blocks, "ACME")

    @classmethod
    def tearDownClass(cls) -> None:
        _restore_default_profile()

    def test_profile_classifies_vendor_headers(self) -> None:
        tables = [
            conv._table_rows(x["table_body"])
            for x in self.blocks if x.get("type") == "table"
        ]
        self.assertEqual(["summary", "bitfield", "enum"], [
            conv._classify_table(rows[0]) for rows in tables
        ])
        self.assertTrue(conv._is_register_name("STATUS"))

    def test_extracts_register_address_and_bit_field(self) -> None:
        reg = next(x for x in self.part["registers"] if x["register_name"] == "ACME_CTRL")
        self.assertEqual("0x100", reg["address"])
        self.assertEqual("ENABLE", reg["bit_fields"][0]["name"])
        self.assertEqual("[0]", reg["bit_fields"][0]["bit_range"])
        self.assertEqual("R/W", reg["bit_fields"][0]["access"])
        self.assertEqual("0", reg["bit_fields"][0]["reset_value"])

    def test_extracts_enum_formula_algorithm_and_figure_context(self) -> None:
        self.assertEqual("ACME_MODE_ENUM", self.part["enums"][0]["name"])
        self.assertEqual("ACME_MODE_RUN", self.part["enums"][0]["values"][1]["mnemonic"])
        self.assertEqual("$$ y = ax + b $$", self.part["formulas"][0]["latex"])
        self.assertIn("MODE = RUN", self.part["algorithms"][0]["text"])
        fig = self.part["figures"][0]
        self.assertEqual("Figure 1-1 ACME Block Diagram", fig["caption"])
        self.assertIn("receives jobs", fig["context"])

    def test_generated_shape_passes_kb_validator(self) -> None:
        kb = {
            "modules": ["ACME"],
            "documents": [],
            **self.part,
        }
        report = validate(kb, FIXTURES)
        self.assertEqual([], report["errors"])

    def test_source_audit_reports_profile_coverage(self) -> None:
        report = audit(
            [FIXTURES / "acme_content_list.json"],
            FIXTURES / "acme_profile.json",
        )
        self.assertEqual({"summary": 1, "bitfield": 1, "enum": 1}, report["table_types"])
        self.assertGreaterEqual(report["documents"][0]["register_anchor_count"], 2)


class FindColAliasPriorityTests(unittest.TestCase):
    """回归测试：全量重跑 Docling 对照时发现，5/7 模块的汇总表地址栏全部丢失。

    根因：`_HEADER_ALIASES["address"] = ["address", "bar"]`，而
    `_find_col` 原实现按"列顺序优先"扫描（对每一列依次判断是否命中任一
    alias），真实手册汇总表常见列序是
    ["Register", "BAR", "Address", "CSRType", "DetailedDescription"]——
    "BAR"（Base Address Register 索引，不是地址）排在"Address"前面，
    于是先命中 BAR 列、把它错当成地址列，而 BAR 列里通常没有 0x.. 数值，
    实际效果就是该寄存器的地址被静默清空。修复后 `_find_col` 改为按
    alias 优先顺序扫描（先把 "address" 在所有列里找一遍，找不到才试
    "bar"），恢复正确列。
    """

    def setUp(self) -> None:
        _restore_default_profile()

    def test_bar_column_does_not_shadow_address_column(self) -> None:
        header = ["Register", "BAR", "Address\u00b9", "CSRType", "DetailedDescription"]
        i_addr = conv._find_col(header, *conv._aliases("address"))
        self.assertEqual(2, i_addr)

    def test_parse_summary_extracts_address_not_bar_index(self) -> None:
        rows = [
            ["Register", "BAR", "Address\u00b9", "CSRType", "DetailedDescription"],
            ["ACME_CTRL", "0", "0x87e040600000", "RW", "control reg"],
        ]
        mapping = conv._parse_summary(rows)
        self.assertEqual("0x87e040600000", mapping.get("ACME_CTRL"))

    def test_bar_alias_still_used_as_fallback_when_no_address_column(self) -> None:
        # 没有独立 Address 列、只有 BAR 列的手册，仍应退回用 BAR 当地址列
        # （保留 "bar" 作为 fallback alias 的原始意图）。
        header = ["Register", "BAR", "Description"]
        self.assertEqual(1, conv._find_col(header, *conv._aliases("address")))


class RegisterTokenGluedHeadingTests(unittest.TestCase):
    """回归测试：Docling 对照发现的"描述文字+寄存器名"粘连丢失空格问题。

    真实案例（RFOE 手册 p84/p78，来自 mineru-work/docling_compare_rfoe 的
    Docling 对照 audit）：MinerU 抽出的标题文本里，说明文字与紧跟的寄存器名
    之间没有空格，例如
    "RFOE RX Aperture SMEM Minimum Address RegistersRFOE(0..6)_RX_APERT_SMEM_MIN"。
    修复前 `_extract_register_token` 按空白分词会把整段说明+寄存器名当成一个
    无法匹配的 token，导致该寄存器锚点丢失、紧随其后的位域表被跳过或错挂到
    上一个寄存器名下。
    """

    def setUp(self) -> None:
        _restore_default_profile()

    def test_extracts_register_name_glued_to_preceding_description(self) -> None:
        cases = {
            "RFOE RX Aperture SMEM Minimum Address RegistersRFOE(0..6)_RX_APERT_SMEM_MIN": (
                "RFOE(0..6)_RX_APERT_SMEM_MIN"
            ),
            "RFOE RX Packet Logger Buffer Address RegistersRFOE(0..6)_RX_PKT_LOGGER(0..1)_ADDR": (
                "RFOE(0..6)_RX_PKT_LOGGER(0..1)_ADDR"
            ),
            "RFOE RX Packet Logger Buffer Configuration RegistersRFOE(0..6)_RX_PKT_LOGGER(0..1)_CFG": (
                "RFOE(0..6)_RX_PKT_LOGGER(0..1)_CFG"
            ),
        }
        for text, expected in cases.items():
            with self.subTest(text=text):
                self.assertEqual(expected, conv._extract_register_token(text))

    def test_does_not_split_a_clean_register_name(self) -> None:
        # 无小写字母粘连时，行为应与之前完全一致。
        self.assertEqual("ACME_CTRL_STATUS", conv._extract_register_token("3.2 ACME_CTRL_STATUS"))
        self.assertEqual("", conv._extract_register_token("just a normal sentence"))

    def test_glued_heading_anchor_attaches_bit_fields_to_correct_register(self) -> None:
        blocks = [
            {
                "type": "text",
                "text_level": 2,
                "text": (
                    "RFOE RX Aperture SMEM Minimum Address Registers"
                    "RFOE(0..6)_RX_APERT_SMEM_MIN"
                ),
                "page_idx": 84,
            },
            {
                "type": "table",
                "table_body": (
                    "<table><tr><td>Bit Pos</td><td>Field Name</td><td>Access</td>"
                    "<td>ResetValue</td><td>TypicalValue</td><td>Field Description</td></tr>"
                    "<tr><td>&lt;25:0&gt;</td><td>ADDR</td><td>R/W</td><td>0x0</td><td></td>"
                    "<td>Lowest allowed SMEM byte address.</td></tr></table>"
                ),
                "page_idx": 84,
            },
        ]
        part = conv._convert_doc(blocks, "RFOE")
        names = [r["register_name"] for r in part["registers"]]
        self.assertIn("RFOE(0..6)_RX_APERT_SMEM_MIN", names)
        reg = next(r for r in part["registers"] if r["register_name"] == "RFOE(0..6)_RX_APERT_SMEM_MIN")
        self.assertEqual("ADDR", reg["bit_fields"][0]["name"])


if __name__ == "__main__":
    unittest.main()
