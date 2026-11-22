"""精确概率与霍尔姆校正的口径测试，仅依赖标准库。"""

import sys
import unittest
from fractions import Fraction
from itertools import combinations
from math import comb
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from backend.src.domain.stats import (
    exact_one_sided_pvalue,
    holm_adjust,
    hypergeom_pmf,
    stratified_distribution,
)


def brute_stratified_pvalue(strata, observed):
    """逐层枚举所有标记分配，直接统计命中数 >= observed 的比例。"""
    favorable = 0

    def recurse(layer, carried_hits):
        nonlocal favorable
        if layer == len(strata):
            if carried_hits >= observed:
                favorable += 1
            return
        n, n1, k = strata[layer]
        # 枚举该层霉斑组占据的位置，统计其中落在命中集合（前 k 个位置）的数量。
        for chosen in combinations(range(n), n1):
            hits = sum(1 for position in chosen if position < k)
            recurse(layer + 1, carried_hits + hits)

    recurse(0, 0)
    ways = 1
    for n, n1, _ in strata:
        ways *= comb(n, n1)
    return Fraction(favorable, ways)


class HypergeomTests(unittest.TestCase):
    def test_pmf_sums_to_one_and_matches_formula(self):
        pmf = hypergeom_pmf(10, 4, 6)
        self.assertEqual(sum(pmf.values()), Fraction(1))
        self.assertEqual(pmf[2], Fraction(comb(6, 2) * comb(4, 2), comb(10, 4)))
        self.assertEqual(set(pmf), set(range(0, 5)))

    def test_pmf_support_bounds(self):
        # k 很小、负组人数多，下界为 0。
        self.assertEqual(set(hypergeom_pmf(8, 5, 2)), {0, 1, 2})
        # 命中数多到负组装不下全部未命中，下界抬升。
        self.assertEqual(set(hypergeom_pmf(8, 5, 7)), {4, 5})

    def test_degenerate_strata_are_certain(self):
        self.assertEqual(hypergeom_pmf(5, 5, 3), {3: Fraction(1)})
        self.assertEqual(hypergeom_pmf(5, 0, 3), {0: Fraction(1)})
        self.assertEqual(hypergeom_pmf(4, 2, 0), {0: Fraction(1)})
        self.assertEqual(hypergeom_pmf(4, 2, 4), {2: Fraction(1)})

    def test_invalid_inputs(self):
        with self.assertRaises(ValueError):
            hypergeom_pmf(0, 0, 0)
        with self.assertRaises(ValueError):
            hypergeom_pmf(5, 6, 1)
        with self.assertRaises(ValueError):
            hypergeom_pmf(5, 2, 6)


class StratifiedPValueTests(unittest.TestCase):
    def test_single_stratum_matches_brute_force(self):
        strata = [(10, 4, 6)]
        for observed in range(5):
            self.assertEqual(exact_one_sided_pvalue(strata, observed),
                             brute_stratified_pvalue(strata, observed))

    def test_multiple_strata_match_brute_force(self):
        strata = [(6, 2, 3), (8, 5, 4), (5, 1, 2), (4, 3, 4)]
        distribution = stratified_distribution(strata)
        self.assertEqual(sum(distribution.values()), Fraction(1))
        observed_total = 2 + 4 + 1 + 3
        self.assertEqual(exact_one_sided_pvalue(strata, observed_total),
                         brute_stratified_pvalue(strata, observed_total))
        for observed in range(0, observed_total + 1):
            self.assertEqual(exact_one_sided_pvalue(strata, observed),
                             brute_stratified_pvalue(strata, observed))

    def test_tail_is_monotone(self):
        strata = [(7, 3, 5), (9, 4, 2)]
        previous = Fraction(2)
        for observed in range(8):
            current = exact_one_sided_pvalue(strata, observed)
            self.assertLessEqual(current, previous)
            previous = current

    def test_zero_observed_is_certain_and_extreme_is_small(self):
        strata = [(10, 4, 5), (6, 3, 2)]
        self.assertEqual(exact_one_sided_pvalue(strata, 0), Fraction(1))
        extreme = exact_one_sided_pvalue(strata, 7)
        self.assertEqual(extreme, brute_stratified_pvalue(strata, 7))
        self.assertLess(extreme, Fraction(1, 100))

    def test_deterministic_strata(self):
        # 每层霉斑组必然命中全部条件时，观察到该数量的概率为 1。
        strata = [(5, 2, 5), (4, 3, 4)]
        self.assertEqual(exact_one_sided_pvalue(strata, 5), Fraction(1))


class HolmTests(unittest.TestCase):
    def test_stepdown_order_and_capping(self):
        pvalues = [Fraction(1, 1000), Fraction(1, 100), Fraction(1, 5), Fraction(9, 10)]
        adjusted = holm_adjust(pvalues)
        self.assertEqual(adjusted[0], Fraction(4, 1000))
        self.assertEqual(adjusted[1], Fraction(3, 100))
        self.assertEqual(adjusted[2], Fraction(2, 5))
        # 末位的运行最大值停留在 0.9，4 * 0.9 不改变包络，故为 0.9。
        self.assertEqual(adjusted[3], Fraction(9, 10))

    def test_monotone_envelope(self):
        pvalues = [Fraction(2, 100), Fraction(1, 100), Fraction(1, 100), Fraction(3, 100)]
        adjusted = holm_adjust(pvalues)
        # 排序后第二、三名原始概率相同，校正值取包络且保持单调。
        self.assertEqual(adjusted[1], adjusted[2])
        for raw, fixed in zip(pvalues, adjusted):
            self.assertGreaterEqual(fixed, raw)

    def test_capping_at_one(self):
        pvalues = [Fraction(4, 10), Fraction(4, 10)]
        adjusted = holm_adjust(pvalues)
        self.assertEqual(adjusted, [Fraction(4, 5), Fraction(4, 5)])
        pvalues = [Fraction(6, 10)]
        self.assertEqual(holm_adjust(pvalues), [Fraction(6, 10)])
        pvalues = [Fraction(1, 100), Fraction(9, 10)]
        adjusted = holm_adjust(pvalues)
        self.assertEqual(adjusted[1], Fraction(9, 10))  # 包络不超过 1

    def test_empty_and_single(self):
        self.assertEqual(holm_adjust([]), [])
        self.assertEqual(holm_adjust([Fraction(1, 3)]), [Fraction(1, 3)])


if __name__ == "__main__":
    unittest.main()
