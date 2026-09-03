"""候选视频的可解释综合评分。"""

from __future__ import annotations

from datetime import datetime
from math import log1p
from typing import Any

from .quality_gate import published_age_days


def _score(value: Any) -> float:
    """将模型或配置数值限制在 0 到 1 之间。"""

    try:
        return max(0.0, min(1.0, float(value)))
    except (TypeError, ValueError):
        return 0.0


def _count_score(value: Any, target: int) -> float:
    """以对数曲线归一化互动量，避免少量爆款碾压全部普通优质视频。"""

    try:
        count = max(0, int(value))
    except (TypeError, ValueError):
        return 0.0
    normalized_target = max(1, int(target))
    return min(1.0, log1p(count) / log1p(normalized_target))


def _metric(metrics: dict[str, Any], key: str) -> int | None:
    """读取抓取阶段保存的互动数；未知值不得伪造为热度。"""

    value = metrics.get(key)
    if isinstance(value, bool) or value is None:
        return None
    try:
        return max(0, int(value))
    except (TypeError, ValueError):
        return None


def calculate_candidate_score(
    *,
    metrics: dict[str, Any],
    published_at: str,
    ai_score: Any,
    settings: Any,
    now: datetime,
) -> dict[str, Any]:
    """合成互动、时效与 AI 内容判断，返回可持久化的分项明细。"""

    age_days = published_age_days(published_at, now)
    recency_window_days = max(1, int(settings.recency_window_days))
    recency_score = (
        max(0.0, 1.0 - age_days / recency_window_days)
        if age_days is not None and age_days >= 0
        else 0.0
    )
    scores = {
        "点赞": _count_score(_metric(metrics, "like_count"), settings.like_target),
        "评论": _count_score(_metric(metrics, "comment_count"), settings.comment_target),
        "收藏": _count_score(_metric(metrics, "collect_count"), settings.collect_target),
        "转发": _count_score(_metric(metrics, "share_count"), settings.share_target),
        "时效": recency_score,
        "AI 内容": _score(ai_score),
    }
    weights = {
        "点赞": max(0.0, float(settings.like_weight)),
        "评论": max(0.0, float(settings.comment_weight)),
        "收藏": max(0.0, float(settings.collect_weight)),
        "转发": max(0.0, float(settings.share_weight)),
        "时效": max(0.0, float(settings.recency_weight)),
        "AI 内容": max(0.0, float(settings.ai_weight)),
    }
    total_weight = sum(weights.values())
    if total_weight <= 0:
        raise ValueError("候选评分权重之和必须大于 0")
    contribution = {name: scores[name] * weights[name] / total_weight for name in scores}
    return {
        "score": sum(contribution.values()),
        "ai_score": scores["AI 内容"],
        "data_score": sum(value for name, value in contribution.items() if name != "AI 内容"),
        "age_days": age_days,
        "metrics": {
            "like_count": _metric(metrics, "like_count"),
            "comment_count": _metric(metrics, "comment_count"),
            "collect_count": _metric(metrics, "collect_count"),
            "share_count": _metric(metrics, "share_count"),
        },
        "scores": scores,
        "weights": weights,
        "contribution": contribution,
    }


def calculate_candidate_data_score(
    *,
    metrics: dict[str, Any],
    published_at: str,
    settings: Any,
    now: datetime,
) -> dict[str, Any]:
    """只按互动量和发布时间计算手动搜索的数据分。

    手动 ``/抖音`` 需要快速反馈，因此不为每个搜索候选调用模型。这里会将
    点赞、评论、收藏、转发和时效的权重重新归一化，返回 0 到 1 的可比较分数。
    """

    detailed = calculate_candidate_score(
        metrics=metrics,
        published_at=published_at,
        ai_score=0.0,
        settings=settings,
        now=now,
    )
    data_names = ("点赞", "评论", "收藏", "转发", "时效")
    data_weight_total = sum(detailed["weights"][name] for name in data_names)
    if data_weight_total <= 0:
        raise ValueError("手动抖音数据评分权重之和必须大于 0")
    contribution = {
        name: detailed["scores"][name] * detailed["weights"][name] / data_weight_total
        for name in data_names
    }
    contribution["AI 内容"] = 0.0
    score = sum(contribution.values())
    detailed.update(
        score=score,
        ai_score=0.0,
        data_score=score,
        contribution=contribution,
    )
    return detailed
