"""候选综合评分的确定性测试。"""

from __future__ import annotations

from datetime import datetime
from importlib import import_module

from test_quality_gate import _load_quality_gate


_load_quality_gate()
scoring = import_module("maibot_plugin_douyin_surf.scoring")


class _Settings:
    like_weight = 0.18
    comment_weight = 0.12
    collect_weight = 0.12
    share_weight = 0.13
    recency_weight = 0.10
    ai_weight = 0.35
    like_target = 100000
    comment_target = 10000
    collect_target = 10000
    share_target = 10000
    recency_window_days = 30


def test_high_interaction_and_recent_content_scores_higher() -> None:
    now = datetime(2026, 9, 3, 18, 0, 0)
    strong = scoring.calculate_candidate_score(
        metrics={"like_count": 100000, "comment_count": 10000, "collect_count": 10000, "share_count": 10000},
        published_at="2026-09-03T08:00:00+08:00",
        ai_score=0.8,
        settings=_Settings(),
        now=now,
    )
    weak = scoring.calculate_candidate_score(
        metrics={"like_count": 100, "comment_count": 0, "collect_count": 0, "share_count": 0},
        published_at="2026-07-01T08:00:00+08:00",
        ai_score=0.8,
        settings=_Settings(),
        now=now,
    )

    assert strong["score"] > weak["score"]
    assert strong["age_days"] == 0
    assert round(strong["contribution"]["AI 内容"], 6) == 0.28


def test_unknown_metrics_do_not_gain_data_points() -> None:
    result = scoring.calculate_candidate_score(
        metrics={},
        published_at="",
        ai_score=1.0,
        settings=_Settings(),
        now=datetime(2026, 9, 3, 18, 0, 0),
    )

    assert result["data_score"] == 0.0
    assert result["score"] == 0.35


def test_manual_data_score_does_not_include_ai_weight() -> None:
    result = scoring.calculate_candidate_data_score(
        metrics={"like_count": 100000, "comment_count": 10000, "collect_count": 10000, "share_count": 10000},
        published_at="2026-09-03T08:00:00+08:00",
        settings=_Settings(),
        now=datetime(2026, 9, 3, 18, 0, 0),
    )

    assert result["score"] == 1.0
    assert result["data_score"] == 1.0
    assert result["ai_score"] == 0.0
    assert result["contribution"]["AI 内容"] == 0.0
