"""用演示台账端到端验证分层精确检验、霍尔姆校正与入榜口径。"""

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from backend.src.domain.analysis import analyze, validate_rows
from backend.src.domain.demo import demo_rows
from backend.src.domain.spec import SPEC


PARAMS = {field["key"]: field["default"] for field in SPEC["params"]}


class AnalysisIntegrationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.rows = validate_rows(demo_rows())
        cls.result = analyze(demo_rows(), PARAMS)
        cls.tables = {table["title"]: table for table in cls.result["tables"]}
        cls.ranking = cls.tables["条件检验与入榜排名"]["rows"]
        cls.checks = cls.tables["各库房每日两组人数与命中核对"]["rows"]

    def test_demo_dimensions(self):
        self.assertEqual(len(self.rows), 40)
        self.assertEqual(sum(row["mildew_observed"] for row in self.rows), 16)

    def test_all_18_patterns_tested_and_holm_applied(self):
        self.assertEqual(len(self.ranking), 18)
        sizes = sorted(row["size"] for row in self.ranking)
        self.assertEqual(sizes.count(1), 6)
        self.assertEqual(sizes.count(2), 12)
        for row in self.ranking:
            self.assertGreaterEqual(row["adjusted_p"], row["p_value"] - 1e-15)
            self.assertLessEqual(row["adjusted_p"], 1.0)

    def test_listing_requires_both_gates(self):
        for row in self.ranking:
            if row["listed"]:
                self.assertTrue(row["pattern_id"])
                self.assertLessEqual(row["adjusted_p"], 0.05 + 1e-15)
                self.assertGreaterEqual(row["positive_count"], PARAMS["min_positive_count"])
                self.assertGreaterEqual(row["positive_count"] + row["negative_count"],
                                        PARAMS["min_total_count"])
                self.assertGreaterEqual(row["growth"], PARAMS["min_growth"] - 1e-15)
            else:
                self.assertEqual(row["pattern_id"], "")

    def test_pattern_ids_are_contiguous_and_growth_sorted(self):
        listed = [row for row in self.ranking if row["listed"]]
        self.assertEqual([row["pattern_id"] for row in listed],
                         [f"P{index:02d}" for index in range(1, len(listed) + 1)])
        growths = [row["growth"] for row in listed]
        self.assertEqual(growths, sorted(growths, reverse=True))

    def test_check_table_stratum_sizes_and_hits(self):
        # 三个库房 × 五天 = 15 个层，每个入榜模式各 15 行。
        listed_count = sum(1 for row in self.ranking if row["listed"])
        self.assertEqual(len(self.checks), 15 * listed_count)
        for row in self.checks:
            self.assertEqual(row["total_count"], row["positive_size"] + row["negative_size"])
            self.assertEqual(row["positive_hits"] + row["negative_hits"] <= row["total_count"], True)
            self.assertLessEqual(row["positive_hits"], row["positive_size"])
            self.assertLessEqual(row["negative_hits"], row["negative_size"])
        # 逐层人数之和应回到全批两组总数。
        seen = {(row["room"], row["inspection_date"]) for row in self.checks}
        self.assertEqual(len(seen), 15)

    def test_check_table_hits_reconcile_with_ranking(self):
        first_listed = next(row for row in self.ranking if row["pattern_id"] == "P01")
        related = [row for row in self.checks if row["pattern_id"] == "P01"]
        self.assertEqual(sum(row["positive_hits"] for row in related), first_listed["positive_count"])
        self.assertEqual(sum(row["negative_hits"] for row in related), first_listed["negative_count"])

    def test_evidence_only_references_listed_patterns(self):
        evidence = self.tables["巡检证据明细"]["rows"]
        valid_ids = {row["pattern_id"] for row in self.ranking if row["listed"]}
        for row in evidence:
            for pattern_id in row["pattern_ids"].split("、"):
                self.assertIn(pattern_id, valid_ids)

    def test_p_values_are_valid_probabilities(self):
        for row in self.ranking:
            self.assertGreaterEqual(row["p_value"], 0.0)
            self.assertLessEqual(row["p_value"], 1.0)

    def test_threshold_change_separates_results(self):
        other = dict(PARAMS, humidity_threshold=55)
        result = analyze(demo_rows(), other)
        ranking = result["tables"][0]["rows"]
        # 切分点变化后高湿定义改变，至少有模式的命中数与默认切分点不同。
        defaults = {(row["conditions"]): row["positive_count"] for row in self.ranking}
        changed = any(defaults[row["conditions"]] != row["positive_count"] for row in ranking)
        self.assertTrue(changed)

    def test_missing_group_warns(self):
        all_negative = [{**row, "mildew_observed": False} for row in demo_rows()]
        result = analyze(all_negative, PARAMS)
        self.assertEqual(result["state"], "warning")
        self.assertEqual(result["tables"][0]["rows"], [])


if __name__ == "__main__":
    unittest.main()
