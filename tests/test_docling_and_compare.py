#!/usr/bin/env python3
"""Docling 适配层与后端对比：零 Docling 安装依赖的厂商中立测试。"""

from __future__ import annotations

import json
import sys
import unittest
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parent.parent
EXTRACT = ROOT / "extract"
FIXTURES = Path(__file__).resolve().parent / "fixtures"
sys.path.insert(0, str(EXTRACT))

import compare_backends  # noqa: E402
import docling_to_kb as dl  # noqa: E402
import mineru_to_kb as conv  # noqa: E402


class _FakeProv:
    def __init__(self, page_no: int) -> None:
        self.page_no = page_no


class _FakeCell:
    def __init__(self, text: str) -> None:
        self.text = text


class _FakeTableData:
    def __init__(self, grid: list[list[_FakeCell]]) -> None:
        self.grid = grid


class _FakeTable:
    def __init__(self, rows: list[list[str]], page: int = 1) -> None:
        self.label = SimpleNamespace(value="table")
        self.prov = [_FakeProv(page)]
        self.data = _FakeTableData([[_FakeCell(c) for c in row] for row in rows])
        self.text = ""


class _FakeText:
    def __init__(self, text: str, label: str, level: int | None = None, page: int = 1) -> None:
        self.text = text
        self.label = SimpleNamespace(value=label)
        self.level = level
        self.prov = [_FakeProv(page)]


class _FakeDoc:
    def __init__(self, items: list) -> None:
        self._items = items

    def iterate_items(self):
        for it in self._items:
            yield it, getattr(it, "level", 1) or 1


class DoclingBridgeTests(unittest.TestCase):
    def test_rows_to_html_roundtrip_via_mineru_parser(self) -> None:
        rows = [["Register Name", "Offset"], ["ACME_CTRL", "0x100"]]
        html_table = dl.rows_to_html(rows)
        parsed = conv._table_rows(html_table)
        self.assertEqual(rows, parsed)

    def test_fake_docling_to_content_list_and_kb(self) -> None:
        conv._apply_profile(FIXTURES / "acme_profile.json")
        doc = _FakeDoc([
            _FakeText("1 ACME Peripheral", "section_header", level=1, page=1),
            _FakeText("The ACME block receives jobs.", "paragraph", page=1),
            _FakeTable([
                ["Register Name", "Offset"],
                ["ACME_CTRL", "0x100"],
            ], page=2),
            _FakeText("1.1 ACME_CTRL", "section_header", level=2, page=2),
            _FakeTable([
                ["Bits", "Field", "Type", "Default", "Meaning"],
                ["[0]", "ENABLE", "R/W", "0", "Enable the block."],
            ], page=2),
        ])
        blocks = dl.docling_document_to_content_list(doc)
        self.assertEqual("text", blocks[0]["type"])
        self.assertEqual(1, blocks[0]["text_level"])
        self.assertEqual(0, blocks[0]["page_idx"])  # 1-based → 0-based
        self.assertEqual("table", blocks[2]["type"])
        part = conv._convert_doc(blocks, "ACME")
        reg = next(x for x in part["registers"] if x["register_name"] == "ACME_CTRL")
        self.assertEqual("0x100", reg["address"])
        self.assertEqual("ENABLE", reg["bit_fields"][0]["name"])


class CompareBackendsTests(unittest.TestCase):
    def test_compare_identical_kbs(self) -> None:
        kb = {
            "modules": ["ACME"],
            "registers": [{
                "register_name": "ACME_CTRL",
                "address": "0x100",
                "bit_fields": [{"name": "ENABLE"}],
            }],
            "sections": [],
            "enums": [],
            "formulas": [],
            "algorithms": [],
            "figures": [],
            "tables": [],
        }
        report = compare_backends.compare_kbs(kb, kb, "mineru", "docling")
        self.assertEqual(1.0, report["registers"]["jaccard"])
        self.assertEqual(1, report["addresses"]["agree"])
        self.assertEqual(1.0, report["bit_fields"]["mean_jaccard_on_shared_registers"])

    def test_compare_detects_register_gap(self) -> None:
        a = {
            "registers": [{"register_name": "ACME_CTRL", "address": "0x100", "bit_fields": []}],
            "sections": [], "enums": [], "formulas": [], "algorithms": [], "figures": [], "tables": [],
            "modules": ["ACME"],
        }
        b = {
            "registers": [{"register_name": "ACME_STAT", "address": "0x104", "bit_fields": []}],
            "sections": [], "enums": [], "formulas": [], "algorithms": [], "figures": [], "tables": [],
            "modules": ["ACME"],
        }
        report = compare_backends.compare_kbs(a, b, "a", "b")
        self.assertEqual(0.0, report["registers"]["jaccard"])
        self.assertIn("ACME_CTRL", report["registers"]["only_a"])


if __name__ == "__main__":
    unittest.main()
