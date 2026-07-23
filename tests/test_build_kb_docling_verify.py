#!/usr/bin/env python3
"""scripts/build_kb.py 的 --verify-with-docling：模块切片、目标解析、对照落盘。

零 Docling 安装依赖：用 fake DoclingDocument（同 test_docling_and_compare.py 的手法）
monkeypatch 掉真正的 PDF 解析，只测"主库建完后自动跑一次 Docling 对照"这条链路本身。
"""
from __future__ import annotations

import json
import sys
import unittest
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parent.parent
FIXTURES = Path(__file__).resolve().parent / "fixtures"
sys.path.insert(0, str(ROOT / "scripts"))
sys.path.insert(0, str(ROOT / "extract"))

import build_kb  # noqa: E402
import docling_to_kb as dl  # noqa: E402


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


def _fake_acme_doc() -> _FakeDoc:
    return _FakeDoc([
        _FakeText("1 ACME Peripheral", "section_header", level=1, page=1),
        _FakeTable([["Register Name", "Offset"], ["ACME_CTRL", "0x100"]], page=1),
        _FakeText("1.1 ACME_CTRL", "section_header", level=2, page=1),
        _FakeTable(
            [["Bits", "Field", "Type", "Default", "Meaning"],
             ["[0]", "ENABLE", "R/W", "0", "Enable the block."]],
            page=1,
        ),
    ])


class FilterKbByModuleTests(unittest.TestCase):
    def test_filters_case_insensitively_and_keeps_other_collections_empty(self) -> None:
        kb = {
            "modules": ["ACME", "FOO"],
            "registers": [
                {"register_name": "ACME_CTRL", "module": "ACME", "address": "0x100"},
                {"register_name": "FOO_CTRL", "module": "FOO", "address": "0x200"},
            ],
            "sections": [{"section_id": "s1", "module": "acme"}],
        }
        out = build_kb._filter_kb_by_module(kb, "acme")
        self.assertEqual(["ACME"], out["modules"])
        self.assertEqual(["ACME_CTRL"], [r["register_name"] for r in out["registers"]])
        self.assertEqual(1, len(out["sections"]))
        self.assertEqual([], out["enums"])  # 未出现的集合应是空列表而不是缺 key


class ParseDoclingTargetsTests(unittest.TestCase):
    def test_infers_module_from_pdf_stem(self) -> None:
        [(module, path)] = build_kb._parse_docling_targets(["manuals/rfoe.pdf"])
        self.assertEqual("RFOE", module)
        self.assertEqual(Path("manuals/rfoe.pdf"), path)

    def test_explicit_module_override(self) -> None:
        [(module, path)] = build_kb._parse_docling_targets(["ACME=manuals/acme_v2.pdf"])
        self.assertEqual("ACME", module)
        self.assertEqual(Path("manuals/acme_v2.pdf"), path)

    def test_multiple_targets(self) -> None:
        targets = build_kb._parse_docling_targets(["a.pdf", "FOO=b.pdf"])
        self.assertEqual([("A", Path("a.pdf")), ("FOO", Path("b.pdf"))], targets)


class RunDoclingVerificationTests(unittest.TestCase):
    def setUp(self) -> None:
        import tempfile
        self.tmp = tempfile.TemporaryDirectory()
        self.data_dir = Path(self.tmp.name) / "data"
        self.data_dir.mkdir(parents=True)
        self.pdf_path = Path(self.tmp.name) / "acme.pdf"
        self.pdf_path.write_bytes(b"%PDF-1.4 fake")

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def test_writes_report_and_does_not_mutate_main_kb(self) -> None:
        original_convert = dl.convert_pdf_with_docling
        dl.convert_pdf_with_docling = lambda pdf: _fake_acme_doc()
        try:
            main_kb = {
                "modules": ["ACME", "OTHER"],
                "registers": [
                    {"register_name": "ACME_CTRL", "module": "ACME", "address": "0x100",
                     "bit_fields": [{"name": "ENABLE"}]},
                    {"register_name": "OTHER_REG", "module": "OTHER", "address": "0x900"},
                ],
                "sections": [], "enums": [], "formulas": [], "algorithms": [],
                "figures": [], "tables": [],
            }
            before = json.loads(json.dumps(main_kb))  # 深拷贝用于校验"不被修改"

            ok = build_kb._run_docling_verification(
                [("ACME", self.pdf_path)], main_kb, self.data_dir,
                profile=str(FIXTURES / "acme_profile.json"),
            )

            self.assertTrue(ok)
            self.assertEqual(before, main_kb)  # 对照过程不应改动主库

            report_path = self.data_dir / "docling_compare_ACME.json"
            self.assertTrue(report_path.exists())
            report = json.loads(report_path.read_text(encoding="utf-8"))
            # ACME_CTRL 应该在两侧都存在（一致），OTHER_REG 不该出现在这次对照里
            self.assertEqual(1, report["registers"]["intersection"])
            self.assertEqual(1, report["addresses"]["agree"])
        finally:
            dl.convert_pdf_with_docling = original_convert

    def test_missing_pdf_is_skipped_not_fatal(self) -> None:
        missing = Path(self.tmp.name) / "does_not_exist.pdf"
        main_kb = {"modules": [], "registers": [], "sections": [], "enums": [],
                   "formulas": [], "algorithms": [], "figures": [], "tables": []}
        ok = build_kb._run_docling_verification([("GHOST", missing)], main_kb, self.data_dir, profile=None)
        self.assertFalse(ok)
        self.assertFalse((self.data_dir / "docling_compare_GHOST.json").exists())


if __name__ == "__main__":
    unittest.main()
