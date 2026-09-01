from __future__ import annotations

import re
from typing import Any
from urllib.parse import urlparse


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
    """从页面可见文本中提取日期；没有明确日期时返回空字符串。

    抖音作品卡片常同时出现文案、作者和互动数据，不能把整段卡片文字误当作
    发布时间。这里只保留可确定的 ``YYYY-MM-DD`` 或 ``YYYY/MM/DD`` 日期。
    """

    match = re.search(r"\b(20\d{2})[/-](\d{1,2})[/-](\d{1,2})\b", str(value or ""))
    if match is None:
        return ""
    year, month, day = (int(part) for part in match.groups())
    if not 1 <= month <= 12 or not 1 <= day <= 31:
        return ""
    return f"{year:04d}-{month:02d}-{day:02d}"


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
