"""用 SQLite 验证概率缓存的复用、并发冲突处理与中断续跑语义。"""

import os
import sys
import tempfile
import unittest
from fractions import Fraction
from pathlib import Path

BACKEND = Path(__file__).resolve().parents[1] / "backend"
sys.path.insert(0, str(BACKEND))
os.environ.setdefault("DATABASE_URL", "sqlite:////tmp/mildew-test-default.sqlite")
os.environ.setdefault("JWT_SECRET", "test-secret")
os.environ.setdefault("REDIS_URL", "redis://localhost:6379/0")

from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker

from src.config.database import Base
from src.domain.analysis import analyze
from src.domain.demo import demo_rows
from src.domain.spec import SPEC
from src.models import all_models
from src.models.cache import ProbabilityCache
from src.services.probability import ProbabilityResolver


class CacheTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.NamedTemporaryFile(suffix=".sqlite", delete=False)
        self.tmp.close()
        self.engine = create_engine(f"sqlite:///{self.tmp.name}")
        self.session = sessionmaker(self.engine)
        Base.metadata.create_all(self.engine, tables=[model.__table__ for model in all_models])
        self.params = {field["key"]: field["default"] for field in SPEC["params"]}

    def tearDown(self):
        self.engine.dispose()
        os.unlink(self.tmp.name)

    def resolver(self, dataset_id, threshold=65, progress=None):
        return ProbabilityResolver(dataset_id, threshold, progress=progress,
                                   session_factory=self.session)

    def test_first_run_populates_and_second_run_reuses(self):
        resolver = self.resolver(dataset_id=7)
        result = analyze(demo_rows(), self.params, resolver=resolver)
        self.assertEqual(resolver.misses, 18)
        self.assertEqual(resolver.hits, 0)
        with self.session() as db:
            entries = db.scalars(select(ProbabilityCache)).all()
            self.assertEqual(len(entries), 18)
            for entry in entries:
                self.assertEqual(entry.dataset_id, 7)
                self.assertEqual(entry.humidity_threshold, 65)

        # 同批次同切分点再次筛查：全部命中缓存，结果一致。
        second = self.resolver(dataset_id=7)
        reused = analyze(demo_rows(), self.params, resolver=second)
        self.assertEqual(second.hits, 18)
        self.assertEqual(second.misses, 0)
        self.assertEqual([row for row in result["tables"][0]["rows"]],
                         [row for row in reused["tables"][0]["rows"]])

    def test_threshold_change_calculates_separately(self):
        first = self.resolver(dataset_id=3, threshold=65)
        analyze(demo_rows(), self.params, resolver=first)
        second = self.resolver(dataset_id=3, threshold=60)
        analyze(demo_rows(), dict(self.params, humidity_threshold=60), resolver=second)
        self.assertEqual(second.misses, 18)  # 换切分点另算
        with self.session() as db:
            entries = db.scalars(select(ProbabilityCache)).all()
            self.assertEqual({entry.humidity_threshold for entry in entries}, {65, 60})
            self.assertEqual(len(entries), 36)

    def test_dataset_change_does_not_share_cache(self):
        resolver_one = self.resolver(dataset_id=1, threshold=65)
        analyze(demo_rows(), self.params, resolver=resolver_one)
        resolver_two = self.resolver(dataset_id=2, threshold=65)
        analyze(demo_rows(), self.params, resolver=resolver_two)
        self.assertEqual(resolver_two.misses, 18)

    def test_concurrent_duplicate_write_falls_back_to_cache(self):
        resolver = self.resolver(dataset_id=9, threshold=65)
        strata = [(6, 2, 3), (8, 5, 4)]
        # 预先插入同键条目，模拟并发筛查已写入。
        with self.session.begin() as db:
            db.add(ProbabilityCache(dataset_id=9, humidity_threshold=65, pattern_key="x=1",
                                    p_value_num=1, p_value_den=7, observed_hits=4,
                                    strata_count=2))
        value = resolver.resolve("x=1", strata, 4)
        self.assertEqual(value, Fraction(1, 7))
        self.assertEqual(resolver.hits, 1)
        self.assertEqual(resolver.misses, 0)

    def test_resume_after_interruption_reuses_finished_patterns(self):
        seen = []
        resolver = self.resolver(dataset_id=11, threshold=65,
                                 progress=lambda key, value, cached_hit: seen.append((key, cached_hit)))
        analyze(demo_rows(), self.params, resolver=resolver)
        # 模拟中断后重跑：18 项概率已落库，全部走缓存且不重新枚举。
        resumed = self.resolver(dataset_id=11, threshold=65)
        analyze(demo_rows(), self.params, resolver=resumed)
        self.assertEqual(resumed.hits, 18)
        self.assertEqual(resumed.misses, 0)
        self.assertTrue(seen)
        self.assertTrue(all(not cached for _, cached in seen))

    def test_integrity_error_fallback_uses_written_value(self):
        # 表中预先存在同键行（模拟并发方已写入），但首次缓存查询视为未命中，
        # 从而驱动唯一约束冲突 -> 回滚 -> 重新读取缓存的回退路径。
        with self.session.begin() as writer:
            writer.add(ProbabilityCache(
                dataset_id=5, humidity_threshold=65, pattern_key="race=1",
                p_value_num=3, p_value_den=11, observed_hits=2, strata_count=2))

        class RacyResolver(ProbabilityResolver):
            def __init__(self, *args, **kwargs):
                super().__init__(*args, **kwargs)
                self._lookup_count = 0

            def _cached(self, db, pattern_key):
                self._lookup_count += 1
                return None if self._lookup_count == 1 else super()._cached(db, pattern_key)

        resolver = RacyResolver(5, 65, session_factory=self.session)
        value = resolver.resolve("race=1", [(5, 2, 3)], 2)
        self.assertEqual(value, Fraction(3, 11))
        self.assertEqual(resolver.misses, 1)

    def test_exact_fraction_is_persisted(self):
        resolver = self.resolver(dataset_id=13, threshold=65)
        analyze(demo_rows(), self.params, resolver=resolver)
        with self.session() as db:
            entry = db.scalar(select(ProbabilityCache).where(
                ProbabilityCache.pattern_key == "high_humidity=1|vent_blocked=1"))
            self.assertEqual(Fraction(entry.p_value_num, entry.p_value_den),
                             Fraction(44, 19683))
            self.assertEqual(entry.observed_hits, 9)
            self.assertEqual(entry.strata_count, 15)


if __name__ == "__main__":
    unittest.main()
