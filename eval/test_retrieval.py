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


class TopRankBonusTests(unittest.TestCase):
    """Top-rank bonus 默认关闭、不改变现有排序；显式开启时按预期生效。"""

    def test_bonus_defaults_to_off_and_matches_plain_rrf(self) -> None:
        dense = [(0, 0.9), (1, 0.5)]
        lexical = [(2, 8.0), (1, 3.0)]
        plain = HybridIndex._rrf(dense, lexical)
        with_zero_bonus = HybridIndex._rrf(dense, lexical, bonus_rank1=0.0, bonus_rank2_3=0.0)
        self.assertEqual(plain, with_zero_bonus)

    def test_bonus_rank1_applies_once_even_if_top_in_both_lists(self) -> None:
        # doc0 排第 1 的 BM25 和 dense 都命中，只应加一次 bonus_rank1，不叠加两次。
        dense = [(0, 0.9), (1, 0.5)]
        lexical = [(0, 8.0), (1, 3.0)]
        fused = HybridIndex._rrf(dense, lexical, bonus_rank1=0.1, bonus_rank2_3=0.02)
        base = HybridIndex._rrf(dense, lexical)
        self.assertAlmostEqual(base[0] + 0.1, fused[0])

    def test_apply_rank_bonus_can_rescue_top_rank_from_dilution(self) -> None:
        # doc9 在某一路排第 1（如 BM25 精确命中标识符），但融合基础分比"两路都
        # 还行"的 doc1（都排中间名次）略低——没开 bonus 时 doc1 领先；开启后
        # doc9 应反超，因为它拿到了 bonus_rank1，而 doc1 只拿到较小的 bonus_rank2_3。
        fused = {9: 0.030, 1: 0.032}
        best_rank = {9: 0, 1: 1}
        self.assertGreater(fused[1], fused[9])
        HybridIndex._apply_rank_bonus(fused, best_rank, bonus_rank1=0.05, bonus_rank2_3=0.02)
        self.assertGreater(fused[9], fused[1])

    def test_apply_rank_bonus_no_op_when_both_zero(self) -> None:
        fused = {0: 0.1, 1: 0.2}
        best_rank = {0: 0, 1: 3}
        before = dict(fused)
        HybridIndex._apply_rank_bonus(fused, best_rank, bonus_rank1=0.0, bonus_rank2_3=0.0)
        self.assertEqual(before, fused)

    def test_apply_rank_bonus_applies_once_not_per_list(self) -> None:
        # best_rank 只存"该文档在任一路里的最佳名次"，_apply_rank_bonus 按这个
        # 名次只加一次 bonus，不会因为它同时出现在两路里就叠加两次。
        fused = {0: 0.05}
        best_rank = {0: 0}
        HybridIndex._apply_rank_bonus(fused, best_rank, bonus_rank1=0.05, bonus_rank2_3=0.02)
        self.assertAlmostEqual(0.10, fused[0])

    def test_relative_score_bonus_defaults_to_off(self) -> None:
        dense = [(0, 0.8), (1, 0.4)]
        lexical = [(1, 10.0), (2, 5.0)]
        plain = HybridIndex._relative_score(dense, lexical, weights=[0.5, 0.5])
        with_zero_bonus = HybridIndex._relative_score(
            dense, lexical, weights=[0.5, 0.5], bonus_rank1=0.0, bonus_rank2_3=0.0)
        self.assertEqual(plain, with_zero_bonus)

    def test_resolve_bonus_prefers_explicit_over_env(self) -> None:
        import os
        old1 = os.environ.pop("CHIP_FUSION_BONUS_RANK1", None)
        old2 = os.environ.pop("CHIP_FUSION_BONUS_RANK2_3", None)
        try:
            os.environ["CHIP_FUSION_BONUS_RANK1"] = "0.05"
            os.environ["CHIP_FUSION_BONUS_RANK2_3"] = "0.02"
            # 显式传参优先于环境变量
            self.assertEqual((0.1, 0.03), HybridIndex._resolve_bonus(0.1, 0.03))
            # 都不传时读环境变量
            self.assertEqual((0.05, 0.02), HybridIndex._resolve_bonus(None, None))
        finally:
            os.environ.pop("CHIP_FUSION_BONUS_RANK1", None)
            os.environ.pop("CHIP_FUSION_BONUS_RANK2_3", None)
            if old1 is not None:
                os.environ["CHIP_FUSION_BONUS_RANK1"] = old1
            if old2 is not None:
                os.environ["CHIP_FUSION_BONUS_RANK2_3"] = old2

    def test_resolve_bonus_defaults_to_zero_when_unset(self) -> None:
        import os
        old1 = os.environ.pop("CHIP_FUSION_BONUS_RANK1", None)
        old2 = os.environ.pop("CHIP_FUSION_BONUS_RANK2_3", None)
        try:
            self.assertEqual((0.0, 0.0), HybridIndex._resolve_bonus(None, None))
        finally:
            if old1 is not None:
                os.environ["CHIP_FUSION_BONUS_RANK1"] = old1
            if old2 is not None:
                os.environ["CHIP_FUSION_BONUS_RANK2_3"] = old2


if __name__ == "__main__":
    unittest.main()
