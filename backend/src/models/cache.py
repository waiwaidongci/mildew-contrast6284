from datetime import datetime
from sqlalchemy import BigInteger, ForeignKey, String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column
from src.config.database import Base
from src.utils.time import now


class ProbabilityCache(Base):
    """分层精确概率的持久缓存。

    缓存键由数据批次、高湿切分点和条件模式决定：同一批次、同一切分点的并发
    筛查共用已算概率；切分点变化或模式不同则另行计算。后台中断后重跑时，
    命中此表的概率直接复用，不重复枚举。概率以分子/分母整数保存精确值。
    """

    __tablename__ = 'probability_cache'
    __table_args__ = (
        UniqueConstraint('dataset_id', 'humidity_threshold', 'pattern_key',
                         name='uq_probability_cache_scope'),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    dataset_id: Mapped[int] = mapped_column(ForeignKey('datasets.id'), index=True)
    humidity_threshold: Mapped[int] = mapped_column(index=True)
    pattern_key: Mapped[str] = mapped_column(String(120))
    p_value_num: Mapped[int] = mapped_column(BigInteger)
    p_value_den: Mapped[int] = mapped_column(BigInteger)
    observed_hits: Mapped[int]
    strata_count: Mapped[int]
    created_at: Mapped[datetime] = mapped_column(default=now)
