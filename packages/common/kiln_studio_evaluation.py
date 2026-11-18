"""完整留出样本的误差统计与可追溯的绘图明细。"""

from math import isfinite, sqrt


def error_statistics(actual, predicted, *, mape_min_actual=1.0):
    if not isfinite(mape_min_actual) or mape_min_actual <= 0:
        raise ValueError("百分比计算的实测值下限必须为正数")
    pairs = [
        (a, p)
        for a, p in zip(actual, predicted, strict=True)
        if a is not None and p is not None and isfinite(a) and isfinite(p)
    ]
    errors = [p - a for a, p in pairs]
    proportional = [abs((p - a) / a) * 100 for a, p in pairs if abs(a) >= mape_min_actual]
    count = len(pairs)
    return {
        "mae": sum(abs(error) for error in errors) / count if count else None,
        "rmse": sqrt(sum(error * error for error in errors) / count) if count else None,
        "bias": sum(errors) / count if count else None,
        "count": count,
        "totalCount": len(actual),
        "coverage": count / len(actual) if actual else 0,
        "mape": sum(proportional) / len(proportional) if proportional else None,
        "mapeCount": len(proportional),
        "mapeExcludedCount": count - len(proportional),
        "mapeMinActual": mape_min_actual,
        "maxAbsError": max((abs(error) for error in errors), default=None),
    }


def evaluation_detail(rows, algorithm, reference, split, cycle_id, mape_min_actual, radius=None):
    selected = [
        row for row in rows if row["split"] == split and (not cycle_id or row["cycle"] == cycle_id)
    ]
    score = error_statistics(
        [row["actual"] for row in selected],
        [row.get(algorithm) for row in selected],
        mape_min_actual=mape_min_actual,
    )
    points = [
        {
            "origin": row["origin"],
            "ts": row["ts"],
            "cycle": row["cycle"],
            "actual": row["actual"],
            "actualTemperatureSource": row.get("actualTemperatureSource"),
            "prediction": row.get(algorithm),
            "reference": row.get(reference) if reference else None,
            "deviation": row[algorithm] - row["actual"] if row.get(algorithm) is not None else None,
            "lower": row[algorithm] - radius
            if radius is not None and row.get(algorithm) is not None
            else None,
            "upper": row[algorithm] + radius
            if radius is not None and row.get(algorithm) is not None
            else None,
        }
        for row in selected
    ]
    worst_indices = sorted(
        (index for index, point in enumerate(points) if point["deviation"] is not None),
        key=lambda index: abs(points[index]["deviation"]),
        reverse=True,
    )[:10]
    stride = max(1, (len(points) + 899) // 900)
    shown = sorted(set(range(0, len(points), stride)) | set(worst_indices))
    return {
        "metrics": score,
        "points": [points[index] for index in shown],
        "worstPoints": [points[index] for index in worst_indices],
        "from": min((row["ts"] for row in selected), default=None),
        "to": max((row["ts"] for row in selected), default=None),
    }
