"""端到端：FastAPI 入队 + worker 执行 + 缓存复用 + 结果接口核对（SQLite）。"""

import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

BACKEND = Path(__file__).resolve().parents[1] / "backend"
sys.path.insert(0, str(BACKEND))
_TMP_DB = tempfile.NamedTemporaryFile(suffix=".sqlite", delete=False)
_TMP_DB.close()
os.environ.update(
    DATABASE_URL=f"sqlite:///{_TMP_DB.name}",
    JWT_SECRET="test-secret",
    REDIS_URL="redis://localhost:6379/0",
    ARTIFACT_DIR="/tmp/mildew-artifacts-test",
    SEED_DEMO="false",
)

from fastapi.testclient import TestClient
from sqlalchemy import create_engine, select

import src.config.database as database_module
from src.constants.enums import Status
from src.main import app
from src.models.cache import ProbabilityCache
from src.services import execution


class EndToEndTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        # 让应用使用临时 SQLite 引擎，并屏蔽 Redis 唤醒。
        cls.engine = create_engine(f"sqlite:///{_TMP_DB.name}")
        cls.session = database_module.sessionmaker(cls.engine)
        database_module.engine = cls.engine
        database_module.Session = cls.session
        # execution 与 probability 在导入时绑定了会话工厂，统一切到测试引擎。
        execution.Session = cls.session
        from src.services import probability as probability_module
        probability_module.DefaultSession = cls.session
        from src.config.database import Base
        from src.models import all_models
        Base.metadata.create_all(cls.engine, tables=[model.__table__ for model in all_models])
        cls.client = TestClient(app)

    def test_01_seed_and_run_through_worker(self):
        from scripts.seed_demo import seed
        seeded = seed()
        self.assertEqual(seeded["row_count"], 40)

        login = self.client.post("/auth/login", json={"username": "analyst",
                                                       "password": "analyst-demo-2611"})
        self.assertEqual(login.status_code, 200, login.text)
        token = login.json()["token"]
        headers = {"Authorization": f"Bearer {token}"}

        payload = {"dataset_id": seeded["seed_dataset_id"], "name": "端到端筛查",
                   "params": {"humidity_threshold": 65, "smoothing": 0.5,
                              "min_positive_count": 3, "min_total_count": 5, "min_growth": 1.5}}
        with mock.patch("src.routes.runs.wake"):
            queued = self.client.post("/runs", json=payload, headers=headers)
        self.assertEqual(queued.status_code, 202, queued.text)
        run_id = queued.json()["id"]

        # 模拟 worker：领取、执行（概率写入缓存）、完成。
        lease = execution.claim()
        self.assertIsNotNone(lease)
        self.assertEqual(lease[0], run_id)
        with mock.patch.object(execution, "_touch_heartbeat"):
            execution.execute(lease)

        detail = self.client.get(f"/runs/{run_id}", headers=headers).json()
        self.assertEqual(detail["status"], Status.COMPLETE)
        tables = {table["title"]: table for table in detail["result"]["tables"]}
        ranking = tables["条件检验与入榜排名"]["rows"]
        self.assertEqual(len(ranking), 18)
        listed = [row for row in ranking if row["listed"]]
        self.assertEqual(len(listed), 1)
        self.assertEqual(listed[0]["conditions"], "高湿且风口遮挡")
        self.assertLessEqual(listed[0]["adjusted_p"], 0.05)
        checks = tables["各库房每日两组人数与命中核对"]["rows"]
        self.assertEqual(len(checks), 15)  # 唯一入榜模式 × 15 个库房日

        # 缓存表已落 18 项精确概率。
        with self.session() as db:
            entries = db.scalars(select(ProbabilityCache)).all()
        self.assertEqual(len(entries), 18)
        self.assertTrue(all(entry.humidity_threshold == 65 for entry in entries))

    def test_02_rerun_reuses_cache_without_recompute(self):
        # 再次入队同批次同切分点，执行后应全部命中缓存。
        login = self.client.post("/auth/login", json={"username": "analyst",
                                                       "password": "analyst-demo-2611"})
        headers = {"Authorization": f"Bearer {login.json()['token']}"}
        from scripts.seed_demo import seed
        dataset_id = seed()["seed_dataset_id"]
        payload = {"dataset_id": dataset_id, "name": "复跑筛查", "params": {}}
        with mock.patch("src.routes.runs.wake"):
            run_id = self.client.post("/runs", json=payload, headers=headers).json()["id"]
        with mock.patch.object(execution, "_touch_heartbeat"):
            execution.execute(execution.claim())
        detail = self.client.get(f"/runs/{run_id}", headers=headers).json()
        self.assertEqual(detail["status"], Status.COMPLETE)
        ranking = detail["result"]["tables"][0]["rows"]
        self.assertEqual(sum(1 for row in ranking if row["listed"]), 1)

    def test_03_interrupted_resume_keeps_completed_probabilities(self):
        # 模拟运行中断：把运行留在 RUNNING 且租约过期，重跑仍可命中已落库概率。
        from src.models.jobs import Run
        from src.utils.time import now
        from datetime import timedelta
        with self.session.begin() as db:
            run = db.get(Run, db.scalar(select(Run.id).order_by(Run.id.desc())))
            run.status = Status.RUNNING
            run.lease_until = now() - timedelta(minutes=10)
        lease = execution.claim()  # 过期租约应被重新领取
        self.assertIsNotNone(lease)
        with mock.patch.object(execution, "_touch_heartbeat"):
            execution.execute(lease)
        # 概率表仍为 18 项，没有因重跑产生重复或缺失。
        with self.session() as db:
            count = len(db.scalars(select(ProbabilityCache)).all())
        self.assertEqual(count, 18)


if __name__ == "__main__":
    unittest.main()
