"""集中导入全部 ORM 模型，并提供显式注册表的辅助函数。

导入本包即把所有表加入 ``Base.metadata``；``all_models`` 供需要显式引用模型集合
的调用方（如建表、测试夹具）使用，避免出现纯副作用的未用导入告警。
"""

from src.models.cache import ProbabilityCache
from src.models.identity import Audit, User
from src.models.jobs import Delivery, Run, Schedule
from src.models.source import Dataset

all_models = (ProbabilityCache, Audit, User, Delivery, Run, Schedule, Dataset)

__all__ = ["ProbabilityCache", "Audit", "User", "Delivery", "Run", "Schedule",
           "Dataset", "all_models"]
