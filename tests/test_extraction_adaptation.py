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


class ExtractionAdaptationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        conv._apply_profile(FIXTURES / "acme_profile.json")
        cls.blocks = json.loads(
            (FIXTURES / "acme_content_list.json").read_text(encoding="utf-8")
        )
        cls.part = conv._convert_doc(cls.blocks, "ACME")

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


if __name__ == "__main__":
    unittest.main()
