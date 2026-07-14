#!/usr/bin/env python3
"""BM25 分词/轻量词干归一的零依赖单元测试。"""

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "server"))

from retrieval import _BM25, _tokenize  # noqa: E402


class TokenizeTests(unittest.TestCase):
    def test_common_inflections_share_stems(self) -> None:
        self.assertIn("drop", _tokenize("dropped dropping drops"))
        self.assertIn("packet", _tokenize("packets"))
        self.assertIn("receiv", _tokenize("received receives"))

    def test_identifiers_are_preserved(self) -> None:
        tokens = _tokenize("ACME_RX_STAT4 CTRL_STATUS")
        self.assertIn("acme_rx_stat4", tokens)
        self.assertIn("ctrl_status", tokens)
        self.assertIn("stat4", tokens)

    def test_stemming_improves_bm25_recall(self) -> None:
        bm25 = _BM25([
            _tokenize("Packets were dropped because the receive FIFO was full."),
            _tokenize("Port kind and link configuration."),
        ])
        self.assertEqual(0, bm25.top_n("how does ACME drop received packets", 2)[0][0])


if __name__ == "__main__":
    unittest.main()
