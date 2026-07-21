#!/usr/bin/env python3
"""BM25 分词/轻量词干归一的零依赖单元测试。"""

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "server"))

from retrieval import _BM25, _tokenize, HybridIndex  # noqa: E402


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


class FusionTests(unittest.TestCase):
    def test_relative_score_normalizes_by_max(self) -> None:
        dense = [(0, 0.8), (1, 0.4)]
        lexical = [(1, 10.0), (2, 5.0)]
        fused = HybridIndex._relative_score(dense, lexical, weights=[0.5, 0.5])
        # doc0: 0.5*(0.8/0.8)=0.5；doc1: 0.5*(0.4/0.8)+0.5*(10/10)=0.25+0.5=0.75
        self.assertAlmostEqual(0.5, fused[0])
        self.assertAlmostEqual(0.75, fused[1])
        self.assertAlmostEqual(0.25, fused[2])

    def test_resolve_fusion_aliases(self) -> None:
        self.assertEqual("relative_score", HybridIndex._resolve_fusion("relative"))
        self.assertEqual("rrf", HybridIndex._resolve_fusion("rrf"))
        import os
        old = os.environ.pop("CHIP_FUSION", None)
        try:
            self.assertEqual("rrf", HybridIndex._resolve_fusion(None))
        finally:
            if old is not None:
                os.environ["CHIP_FUSION"] = old


if __name__ == "__main__":
    unittest.main()
