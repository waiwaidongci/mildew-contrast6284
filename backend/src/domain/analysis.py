"""对巡检记录分组，计算条件增长倍数、分层精确概率并保留命中证据。"""

from collections import defaultdict
from datetime import date
from fractions import Fraction
from itertools import combinations

from .spec import SPEC
from .stats import holm_adjust


_CONDITION_LABELS = {
    "high_humidity": ("低于高湿切分点", "高湿"),
    "vent_blocked": ("风口未遮挡", "风口遮挡"),
    "maintenance_pending": ("无待结报修", "设施报修待结"),
}
_FIELDS = SPEC["fields"]
# 三个字段各枚举真、假两个取值；单项 6 个，跨字段成对 12 个，共 18 项检验。
_LITERALS = tuple((field, value) for field in _CONDITION_LABELS for value in (False, True))
_PATTERNS = tuple((literal,) for literal in _LITERALS) + tuple(
    pair for pair in combinations(_LITERALS, 2) if pair[0][0] != pair[1][0])
_ALPHA = Fraction(5, 100)
_PATTERN_COLUMNS = (
    ("pattern_id", "模式编号"), ("conditions", "伴随条件"), ("size", "条件数"),
    ("positive_count", "正组命中"), ("negative_count", "负组命中"),
    ("positive_support", "正组原始支持度"), ("negative_support", "负组原始支持度"),
    ("positive_smoothed", "正组平滑支持度"), ("negative_smoothed", "负组平滑支持度"),
    ("growth", "增长倍数"), ("p_value", "确切概率"),
    ("adjusted_p", "霍尔姆校正概率"), ("listed", "入榜"),
)
_STRATUM_COLUMNS = (
    ("room", "库房"), ("inspection_date", "巡检日期"),
    ("total_count", "巡检总数"), ("positive_size", "霉斑组人数"),
    ("negative_size", "未见霉斑组人数"), ("positive_hits", "霉斑组命中数"),
    ("negative_hits", "未见霉斑组命中数"), ("pattern_id", "模式编号"),
    ("conditions", "伴随条件"),
)
_EVIDENCE_COLUMNS = (("pattern_ids", "命中模式编号"),) + tuple(
    (field["key"], field["label"]) for field in _FIELDS)


def _bounded(value, field):
    allowed = (int,) if field["type"] == "int" else (int, float)
    if type(value) not in allowed or not field["min"] <= value <= field["max"]:
        raise ValueError(f"{field['label']}须为 {field['min']} 至 {field['max']} 范围内的有效数值")
    return value


def validate_rows(rows):
    """校验导入与计算共用的巡检口径，返回独立记录。"""
    if not isinstance(rows, (list, tuple)) or not rows:
        raise ValueError("请提供至少一条巡检记录")
    validated = []
    seen = set()
    for index, row in enumerate(rows, start=1):
        if not isinstance(row, dict):
            raise ValueError(f"第 {index} 条巡检记录格式有误")
        record = {}
        for field in _FIELDS:
            key, kind = field["key"], field["type"]
            value = row.get(key)
            try:
                if kind == "string":
                    if not isinstance(value, str) or not 1 <= len(value.strip()) <= field["max_length"]:
                        raise ValueError(f"{field['label']}为空或长度超限")
                    value = value.strip()
                elif kind == "date":
                    if not isinstance(value, str) or date.fromisoformat(value).isoformat() != value:
                        raise ValueError
                elif kind == "bool":
                    if type(value) is not bool:
                        raise ValueError(f"{field['label']}须明确填写是或否")
                else:
                    value = _bounded(value, field)
            except ValueError as error:
                detail = "巡检日期须为有效的 YYYY-MM-DD 日期" if kind == "date" else str(error)
                raise ValueError(f"第 {index} 条巡检记录 · {detail}") from None
            record[key] = value
        inspection_id = record["inspection_id"]
        if inspection_id.casefold() in seen:
            raise ValueError(f"第 {index} 条巡检记录 · 巡检单号「{inspection_id}」重复")
        seen.add(inspection_id.casefold())
        validated.append(record)
    return validated


def _row_literals(row, humidity_threshold):
    return {
        "high_humidity": row["humidity"] >= humidity_threshold,
        "vent_blocked": row["vent_blocked"],
        "maintenance_pending": row["maintenance_pending"],
    }


def _pattern_key(pattern):
    return "|".join(f"{field}={int(value)}" for field, value in pattern)


def _pattern_label(pattern):
    return "且".join(_CONDITION_LABELS[field][value] for field, value in pattern)


def _row_matches(row_literals, pattern):
    return all(row_literals[field] is value for field, value in pattern)


def _build_strata(records):
    """按库房和巡检日固定两层人数，返回有序层列表与记录到层的映射。"""
    grouped = defaultdict(list)
    for index, row in enumerate(records):
        grouped[(row["room"], row["inspection_date"])].append(index)
    strata = []
    row_stratum = {}
    for (room, day), indices in sorted(grouped.items()):
        positive_size = sum(1 for index in indices if records[index]["mildew_observed"])
        strata.append({
            "room": room, "inspection_date": day, "indices": tuple(indices),
            "total_count": len(indices), "positive_size": positive_size,
            "negative_size": len(indices) - positive_size,
        })
        for index in indices:
            row_stratum[index] = len(strata) - 1
    return strata, row_stratum


def _count_pattern(records, strata, pattern, threshold):
    """统计每个（库房、巡检日）层内两组人数与该模式命中数。"""
    per_stratum = []
    positive_total = negative_total = 0
    for stratum in strata:
        positive_hits = negative_hits = 0
        for index in stratum["indices"]:
            if _row_matches(_row_literals(records[index], threshold), pattern):
                if records[index]["mildew_observed"]:
                    positive_hits += 1
                else:
                    negative_hits += 1
        positive_total += positive_hits
        negative_total += negative_hits
        per_stratum.append({
            **{key: stratum[key] for key in ("room", "inspection_date", "total_count",
                                             "positive_size", "negative_size")},
            "positive_hits": positive_hits, "negative_hits": negative_hits,
            # 超几何口径：层内命中该模式的总条数与霉斑组人数固定。
            "k": positive_hits + negative_hits,
        })
    return per_stratum, positive_total, negative_total


def _growth(positive, negative, positives, negatives, alpha):
    positive_smoothed = (positive + alpha) / (positives + 2 * alpha)
    negative_smoothed = (negative + alpha) / (negatives + 2 * alpha)
    return positive_smoothed / negative_smoothed, positive_smoothed, negative_smoothed


def _table(title, columns, rows):
    return {"title": title, "columns": [{"key": key, "label": label} for key, label in columns], "rows": rows}


def analyze(rows, normalized_params, resolver=None):
    """对同一批有效巡检计算分层精确检验与霍尔姆校正后的条件排名。

    resolver 为可选的概率解析器（带持久缓存）。传入时按模式逐个解析概率，
    中断后续跑可复用已落库的概率；不传则在进程内直接精确枚举。
    """
    records = validate_rows(rows)
    params = {
        field["key"]: _bounded(normalized_params.get(field["key"], field["default"]), field)
        for field in SPEC["params"]
    }
    total = len(records)
    positives = sum(row["mildew_observed"] for row in records)
    negatives = total - positives
    threshold = params["humidity_threshold"]
    alpha = Fraction(str(params["smoothing"]))
    minimum_growth = Fraction(str(params["min_growth"]))
    pattern_rows = []
    check_rows = []
    evidence = []
    pattern_ids = {}
    eligible_index = []
    pattern_stats = []
    cache_hits = cache_misses = 0

    if positives and negatives:
        strata, _row_stratum = _build_strata(records)
        pattern_stats = []
        for pattern in _PATTERNS:
            per_stratum, positive, negative = _count_pattern(
                records, strata, pattern, threshold)
            growth, positive_smoothed, negative_smoothed = _growth(
                positive, negative, positives, negatives, alpha)
            hypergeo_strata = [(item["total_count"], item["positive_size"], item["k"])
                               for item in per_stratum]
            if resolver is None:
                from .stats import exact_one_sided_pvalue
                p_value = exact_one_sided_pvalue(hypergeo_strata, positive)
            else:
                p_value = resolver.resolve(_pattern_key(pattern), hypergeo_strata, positive)
            pattern_stats.append({
                "pattern": pattern, "per_stratum": per_stratum, "positive": positive,
                "negative": negative, "growth": growth,
                "positive_smoothed": positive_smoothed, "negative_smoothed": negative_smoothed,
                "p_value": p_value,
            })
        if resolver is not None:
            cache_hits, cache_misses = resolver.hits, resolver.misses

        adjusted = holm_adjust([stats["p_value"] for stats in pattern_stats])
        eligible_index = []
        for index, stats in enumerate(pattern_stats):
            stats["adjusted_p"] = adjusted[index]
            positive, negative = stats["positive"], stats["negative"]
            original_gate = (
                positive >= params["min_positive_count"]
                and positive + negative >= params["min_total_count"]
                and stats["growth"] >= minimum_growth)
            stats["listed_gate"] = original_gate and stats["adjusted_p"] <= _ALPHA
            if stats["listed_gate"]:
                eligible_index.append(index)

        # 入榜模式沿用增长倍数排序编号，便于证据与核对表互相查找。
        eligible_index.sort(key=lambda i: (-pattern_stats[i]["growth"], -pattern_stats[i]["positive"],
                                           len(pattern_stats[i]["pattern"]),
                                           _pattern_key(pattern_stats[i]["pattern"])))
        pattern_ids = {}
        for rank, index in enumerate(eligible_index, start=1):
            pattern_ids[index] = f"P{rank:02d}"

        for index, stats in enumerate(pattern_stats):
            pattern = stats["pattern"]
            pattern_id = pattern_ids.get(index, "")
            pattern_rows.append({
                "pattern_id": pattern_id,
                "conditions": _pattern_label(pattern),
                "size": len(pattern),
                "positive_count": stats["positive"],
                "negative_count": stats["negative"],
                "positive_support": stats["positive"] / positives,
                "negative_support": stats["negative"] / negatives,
                "positive_smoothed": float(stats["positive_smoothed"]),
                "negative_smoothed": float(stats["negative_smoothed"]),
                "growth": float(stats["growth"]),
                "p_value": float(stats["p_value"]),
                "adjusted_p": float(stats["adjusted_p"]),
                "listed": stats["listed_gate"],
            })
            if pattern_id:
                for item in stats["per_stratum"]:
                    check_rows.append({
                        **item, "pattern_id": pattern_id,
                        "conditions": _pattern_label(pattern),
                    })

        listed_patterns = {pattern_stats[i]["pattern"]: pattern_ids[i] for i in eligible_index}
        for row in sorted(records, key=lambda row: (row["inspection_date"], row["inspection_id"])):
            row_literals = _row_literals(row, threshold)
            matching = sorted(pattern_id for pattern, pattern_id in listed_patterns.items()
                              if _row_matches(row_literals, pattern))
            if matching:
                evidence.append({**row, "pattern_ids": "、".join(matching)})

    if not positives or not negatives:
        missing_group = "见霉斑" if not positives else "未见霉斑"
        headline = f"缺少{missing_group}的巡检记录，暂无法比较两组。"
    elif pattern_ids:
        top_index = eligible_index[0]
        headline = f"筛出 {len(pattern_ids)} 个入榜模式，{_pattern_label(pattern_stats[top_index]['pattern'])}的增长倍数最高。"
    else:
        headline = "本批巡检中没有模式同时达到原始门槛且霍尔姆校正概率不超过 5%。"

    note = (
        f"纳入 {total} 条巡检。按库房和巡检日固定霉斑组与未见霉斑组人数，"
        f"在层内随机重排霉斑标记并精确枚举（不抽样），湿度达到 {threshold:g}% 归入高湿，"
        f"平滑量 {params['smoothing']:g}。共检验 {len(_PATTERNS)} 个单项与成对模式，"
        f"全部参与霍尔姆校正；校正概率不超过 5% 且达到正组命中至少 {params['min_positive_count']} 条、"
        f"两组合计至少 {params['min_total_count']} 条、增长至少 {params['min_growth']:g} 倍的原始门槛方可入榜。"
        f"结果反映本批记录的关联，无法据此判定霉斑成因。"
    )
    if cache_hits or cache_misses:
        note += f"本次概率计算复用缓存 {cache_hits} 项、新算 {cache_misses} 项。"

    return {
        "state": "warning" if not positives or not negatives or not pattern_ids else "ok",
        "headline": headline,
        "note": note,
        "metrics": [
            {"label": "见霉斑巡检", "value": positives, "detail": "各库房每日霉斑组人数之和"},
            {"label": "未见霉斑巡检", "value": negatives, "detail": "各库房每日未见霉斑组人数之和"},
            {"label": "检验模式", "value": len(_PATTERNS),
             "detail": f"单项 {len(_LITERALS)} 个，跨字段成对 {len(_PATTERNS) - len(_LITERALS)} 个，全部参与校正"},
            {"label": "入榜模式", "value": len(pattern_ids),
             "detail": "霍尔姆校正概率 ≤ 5% 且达到全部原始门槛"},
            {"label": "命中巡检", "value": len(evidence), "detail": "至少符合一个入榜模式，按巡检单号计数"},
        ],
        "tables": [
            _table("条件检验与入榜排名", _PATTERN_COLUMNS, pattern_rows),
            _table("各库房每日两组人数与命中核对", _STRATUM_COLUMNS, check_rows),
            _table("巡检证据明细", _EVIDENCE_COLUMNS, evidence),
        ],
        "charts": [{"title": "入榜模式增长倍数", "kind": "bar", "series": [
            {"label": row["pattern_id"], "value": row["growth"]}
            for row in pattern_rows if row["pattern_id"]][:8]}] if pattern_ids else [],
        "sections": [{"title": "分层精确检验口径", "text": (
            "每个（库房、巡检日）层内两组人数固定，条件命中总数也固定；把霉斑标记在层内随机重排，"
            "霉斑组命中数服从超几何分布，跨层卷积得到总命中数的确切分布。"
            "确切概率是随机分配后霉斑组命中至少与实际同样多的概率，全程精确枚举、不抽样。"
            "全部单项与成对模式的概率统一做霍尔姆校正，校正概率不超过 5% 且达到原始门槛才入榜。"
        )}, {"title": "按模式复核巡检", "text": (
            "用模式编号在核对表查看各库房每天的两组人数与命中数，再在巡检证据或下载的 CSV 中查找命中记录，"
            "按巡检单号回查台账。同一条巡检可命中多个模式。复核时查看同库房同期记录、连续湿度读数和设施报修单。"
        )}],
    }
