# 002 分层精确概率缓存

- 新增 `probability_cache` 表：按 `(dataset_id, humidity_threshold, pattern_key)` 唯一约束保存每个模式的确切单侧概率。
- 概率以 `p_value_num` / `p_value_den` 两个大整数存储精确分数，另存实际命中数与层数；应用启动时 `Base.metadata.create_all` 会在已有库上自动补建本表，无需手工迁移。
- 同批次同湿度切分点的并发筛查共用本表；切分点或模式不同则另行计算。后台中断后重跑时，已落库的概率直接命中，不重复枚举。
- `runs` 表结构不变；分层口径、霍尔姆校正与入榜结果保存在 `runs.result` 的 JSON 中。
