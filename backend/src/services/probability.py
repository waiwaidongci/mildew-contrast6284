"""精确概率的缓存解析：命中缓存直接复用，未命中则现场精确枚举并落库。"""

import logging
from fractions import Fraction

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from src.config.database import Session as DefaultSession
from src.domain.stats import exact_one_sided_pvalue
from src.models.cache import ProbabilityCache

log = logging.getLogger(__name__)


class ProbabilityResolver:
    """按 (批次, 切分点, 模式) 解析精确单侧概率，带持久缓存与租约续约回调。

    progress 在每个模式处理完成后调用，用于后台中断续跑时刷新租约与心跳。
    会话工厂延迟读取，便于测试替换数据库绑定。
    """

    def __init__(self, dataset_id, humidity_threshold, progress=None, session_factory=None):
        self.dataset_id = dataset_id
        self.humidity_threshold = int(humidity_threshold)
        self.progress = progress
        self.session_factory = session_factory or DefaultSession
        self.hits = 0
        self.misses = 0

    def _cached(self, db, pattern_key):
        entry = db.scalar(select(ProbabilityCache).where(
            ProbabilityCache.dataset_id == self.dataset_id,
            ProbabilityCache.humidity_threshold == self.humidity_threshold,
            ProbabilityCache.pattern_key == pattern_key))
        return Fraction(entry.p_value_num, entry.p_value_den) if entry else None

    def resolve(self, pattern_key, strata, observed_hits):
        Session = self.session_factory
        with Session.begin() as db:
            cached = self._cached(db, pattern_key)
        if cached is not None:
            self.hits += 1
            if self.progress:
                self.progress(pattern_key, cached, cached_hit=True)
            return cached

        # 纯整数枚举，不做抽样。
        p_value = exact_one_sided_pvalue(strata, observed_hits)
        entry = ProbabilityCache(
            dataset_id=self.dataset_id, humidity_threshold=self.humidity_threshold,
            pattern_key=pattern_key, p_value_num=p_value.numerator,
            p_value_den=p_value.denominator, observed_hits=observed_hits,
            strata_count=len(strata))
        db = Session()
        try:
            db.add(entry)
            db.flush()
            db.commit()
        except IntegrityError:
            # 并发筛查可能已写入同一概率，回滚后另开事务以缓存为准，结果一致。
            db.rollback()
            db.close()
            with Session.begin() as fresh:
                cached = self._cached(fresh, pattern_key)
            if cached is None:
                raise
            p_value = cached
        else:
            db.close()
        self.misses += 1
        if self.progress:
            self.progress(pattern_key, p_value, cached_hit=False)
        return p_value
