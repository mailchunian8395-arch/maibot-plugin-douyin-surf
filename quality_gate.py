from __future__ import annotations

from datetime import date, datetime, timedelta
from typing import Any
from urllib.parse import urlparse

import re


def _score(value: Any, default: float = 0.0) -> float:
    try:
        return max(0.0, min(1.0, float(value)))
    except (TypeError, ValueError):
        return max(0.0, min(1.0, default))


def _flag(value: Any) -> bool:
    return value is True or str(value or "").strip().lower() in {"1", "true", "yes", "on"}


def is_douyin_url(url: str) -> bool:
    host = (urlparse(str(url or "")).hostname or "").lower()
    return host == "douyin.com" or host.endswith(".douyin.com")


def is_official_url(url: str) -> bool:
    """抖音内容不依据外站官方域名判断；保留接口以兼容旧候选。"""
    del url
    return False


def published_today(value: str, local_date: str) -> bool:
    """判断页面显示的日期是否为当天，兼容“刚刚”和“几小时前”。"""
    text = str(value or "").strip().lower()
    return bool(text and (local_date[:10] in text or any(token in text for token in ("今天", "刚刚", "小时前", "分钟前", "today"))))


def visible_date(value: str) -> str:
    """从页面可见文本中提取发布时间；没有明确日期时返回空字符串。

    抖音作品卡片常同时出现文案、作者和互动数据，不能把整段卡片文字误当作
    发布时间。保留可确定的完整日期，以及抖音卡片常见的相对日期，供候选
    最远天数筛选统一判断。
    """

    text = str(value or "")
    match = re.search(r"\b(20\d{2})[/-](\d{1,2})[/-](\d{1,2})\b", text)
    if match is not None:
        year, month, day = (int(part) for part in match.groups())
        if 1 <= month <= 12 and 1 <= day <= 31:
            return f"{year:04d}-{month:02d}-{day:02d}"
    match = re.search(r"\b(20\d{2})年(\d{1,2})月(\d{1,2})日\b", text)
    if match is not None:
        year, month, day = (int(part) for part in match.groups())
        if 1 <= month <= 12 and 1 <= day <= 31:
            return f"{year:04d}-{month:02d}-{day:02d}"
    match = re.search(r"\b(\d+)\s*(分钟|小时|天|周)前\b", text)
    if match is not None:
        return f"{match.group(1)}{match.group(2)}前"
    match = re.search(r"昨天|昨日|刚刚|今天", text)
    return match.group(0) if match is not None else ""


def published_within_days(value: str, max_age_days: int, now: datetime) -> bool:
    """严格判断作品发布时间是否在指定自然日范围内。

    ``0`` 表示不启用门槛。启用后必须能够解析发布时间；日期未知的候选
    不能绕过时效筛选混入候选库。
    """

    limit = max(0, int(max_age_days))
    if limit == 0:
        return True
    published_date = _parse_published_date(value, now)
    if published_date is None:
        return False
    age_days = (now.date() - published_date).days
    return 0 <= age_days <= limit


def _parse_published_date(value: str, now: datetime) -> date | None:
    """解析页面响应 ISO 时间、完整日期与抖音页面可见的相对时间。"""

    text = str(value or "").strip()
    if not text:
        return None
    normalized = text.lower()
    if any(token in normalized for token in ("今天", "刚刚", "分钟前", "小时前", "today", "minutes ago", "hours ago")):
        return now.date()
    if "昨天" in normalized or "昨日" in normalized or "yesterday" in normalized:
        return now.date() - timedelta(days=1)
    match = re.search(r"(\d+)\s*天前", normalized)
    if match is not None:
        return now.date() - timedelta(days=int(match.group(1)))
    match = re.search(r"(\d+)\s*周前", normalized)
    if match is not None:
        return now.date() - timedelta(days=int(match.group(1)) * 7)
    try:
        timestamp = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        timestamp = None
    if timestamp is not None:
        return timestamp.astimezone().date() if timestamp.tzinfo is not None else timestamp.date()
    for pattern in (r"\b(20\d{2})[/-](\d{1,2})[/-](\d{1,2})\b", r"\b(20\d{2})年(\d{1,2})月(\d{1,2})日\b"):
        match = re.search(pattern, text)
        if match is None:
            continue
        try:
            return date(*(int(part) for part in match.groups()))
        except ValueError:
            return None
    return None


def published_age_days(value: str, now: datetime) -> int | None:
    """返回相对今天的自然日数；未知或未来日期不参与数据评分。"""

    published_date = _parse_published_date(value, now)
    if published_date is None:
        return None
    age_days = (now.date() - published_date).days
    return age_days if age_days >= 0 else None


def apply_deep_quality_gate(discovery: dict[str, Any], parsed: dict[str, Any], full_text: str, *, local_date: str) -> dict[str, Any]:
    """对深读结果做轻量、通用的质量和安全门槛校验。"""
    del discovery, full_text, local_date
    quality = _score(parsed.get("content_quality_score"), 0.5)
    share_score = _score(parsed.get("share_score"), quality)
    heat_score = _score(parsed.get("heat_score"))
    # ``official_today`` 只影响分享文案的措辞，不参与是否可分享的判断。
    # 深读提示词未要求模型必填这个字段，因此这里必须提供确定的默认值。
    official_today = _flag(parsed.get("official_today"))
    unsafe = _flag(parsed.get("unsafe"))
    share_eligible = bool(parsed.get("share_worthy", share_score >= 0.7)) and quality >= 0.55 and not unsafe
    if not share_eligible:
        share_score = min(share_score, 0.69)
    return {
        "share_score": share_score,
        "heat_score": heat_score,
        "official_today": official_today,
        "share_eligible": share_eligible,
        "quality_reason": "内容质量或安全条件未达到分享要求" if not share_eligible else "",
    }
