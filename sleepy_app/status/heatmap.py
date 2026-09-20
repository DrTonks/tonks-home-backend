"""Extracted compatibility-preserving domain functions; no startup side effects."""

def _percentile_thresholds(values):
    """从非零值列表计算 25/50/75 百分位阈值"""
    nonzero = sorted(v for v in values if v > 0)
    if not nonzero:
        return 0, 0, 0
    n = len(nonzero)
    return nonzero[int(n * 0.25)], nonzero[int(n * 0.50)], nonzero[int(n * 0.75)]

def _intensity_level(value, p25, p50, p75):
    """根据百分位阈值将数值映射到 0-4 强度等级"""
    if value == 0:
        return 0
    if value <= p25:
        return 1
    if value <= p50:
        return 2
    if value <= p75:
        return 3
    return 4

def calc_heatmap_intensity(activities):
    """基于 messageCount 的百分位计算 0-4 强度等级"""
    if not activities:
        return activities
    p25, p50, p75 = _percentile_thresholds(
        a.get('messageCount', 0) for a in activities
    )
    result = []
    for a in activities:
        entry = dict(a)
        entry['intensity'] = _intensity_level(entry.get('messageCount', 0), p25, p50, p75)
        result.append(entry)
    return result
