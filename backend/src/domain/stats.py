"""按库房和巡检日分层的精确概率与霍尔姆多重比较校正。

全部计算使用整数计数与 Fraction，精确枚举随机分配霉斑标记后的命中分布，
不做蒙特卡洛抽样。每个（库房、巡检日）层内，见霉斑与未见霉斑的人数固定，
条件命中数 K 在该层全部巡检中同样固定；把霉斑标记在层内随机重排时，
霉斑组命中数服从超几何分布，跨层卷积即得到总命中数的精确分布。

动态规划以「统一分母下的整数权重」保存各命中数的组合数，避免逐项 Fraction
反复约分，五千条记录也能在纯 Python 下完成精确枚举。
"""

from fractions import Fraction
from math import comb


def hypergeom_weights(n, n1, k):
    """单层超几何分布的整数权重。

    返回 (lower, weights, denominator)：霉斑组命中数 x 对应权重
    comb(k, x)*comb(n-k, n1-x)，取值区间 lower..upper；
    denominator = comb(n, n1) 是该层全部标记分配数。
    """
    if n <= 0 or not 0 <= n1 <= n or not 0 <= k <= n:
        raise ValueError("分层人数或命中数越界")
    lower = max(0, n1 - (n - k))
    upper = min(n1, k)
    weights = [comb(k, x) * comb(n - k, n1 - x) for x in range(lower, upper + 1)]
    return lower, weights, comb(n, n1)


def hypergeom_pmf(n, n1, k):
    """单层超几何概率 {x: Fraction}，供需要概率分布的调用方与测试使用。"""
    lower, weights, denominator = hypergeom_weights(n, n1, k)
    return {lower + index: Fraction(weight, denominator)
            for index, weight in enumerate(weights)}


def _integer_distribution(strata):
    """跨层卷积，返回 (lower, weights, denominator_product)。

    weights[i] 是霉斑组总命中数为 lower+i 的组合数；所有层的分配等概率且独立，
    分母为各层 comb(n, n1) 之积。
    """
    lower = 0
    weights = [1]
    denominator = 1
    for n, n1, k in strata:
        layer_lower, layer_weights, layer_den = hypergeom_weights(n, n1, k)
        denominator *= layer_den
        convolved = [0] * (len(weights) + len(layer_weights) - 1)
        for index, weight in enumerate(weights):
            if weight == 0:
                continue
            for offset, layer_weight in enumerate(layer_weights):
                convolved[index + offset] += weight * layer_weight
        lower += layer_lower
        weights = convolved
    return lower, weights, denominator


def stratified_distribution(strata):
    """跨层卷积，返回总霉斑组命中数的精确概率分布 {total_hits: Fraction}。"""
    lower, weights, denominator = _integer_distribution(strata)
    return {lower + index: Fraction(weight, denominator)
            for index, weight in enumerate(weights) if weight}


def exact_one_sided_pvalue(strata, observed):
    """随机分配后霉斑组命中数至少为 observed 的确切（单侧）概率。

    observed 是实际数据中霉斑组的总命中数。每层人数与命中数固定，
    因而这里只做精确枚举，不做抽样。概率以 Fraction 返回。
    """
    total_n1 = sum(n1 for _, n1, _ in strata)
    if not 0 <= observed <= total_n1:
        raise ValueError("实际命中数超出霉斑组人数范围")
    lower, weights, denominator = _integer_distribution(strata)
    start = max(0, observed - lower)
    favorable = sum(weights[start:])
    return Fraction(favorable, denominator)


def holm_adjust(pvalues):
    """霍尔姆（Holm-Bonferroni）逐步校正。

    pvalues 为 Fraction 序列，返回与输入等长、同序的校正概率（Fraction，封顶 1）。
    """
    count = len(pvalues)
    order = sorted(range(count), key=lambda index: pvalues[index])
    adjusted = [Fraction(1)] * count
    running = Fraction(0)
    for step, index in enumerate(order):
        candidate = (count - step) * pvalues[index]
        if candidate > running:
            running = candidate
        adjusted[index] = min(running, Fraction(1))
    return adjusted
