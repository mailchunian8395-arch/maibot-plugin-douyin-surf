from __future__ import annotations

import asyncio
import base64
import json
import logging
import random
import re
import time
import uuid
from datetime import datetime, timedelta
from typing import Any
from urllib.parse import urlsplit

from maibot_sdk import Command, EventHandler, HookHandler, MaiBotPlugin
from maibot_sdk.types import ErrorPolicy, EventType, HookMode, HookOrder

from .config_model import ChatSharingRule, DouyinSurfConfig
from .direct_llm import generate_openai_compatible
from .browser_engine import (
    DeepBrowser,
    DouyinSearchAuthenticationError,
    DouyinSearchNoResultError,
)
from .quality_gate import (
    apply_deep_quality_gate,
    is_official_url,
)
from .storage import LifeStore
from .surf_engine import (
    curate_candidates,
    generate_background,
    llm_text,
    parse_json_object,
    select_surf_queries,
    split_source_query,
)
from .video_engine import (
    VideoDurationOutOfRangeError,
    VideoFileTooLargeError,
    download_images_for_share,
    download_short_video_for_share,
    observe_video,
    probe_video_duration,
)

logger = logging.getLogger(__name__)

_DIRECT_API_OPTION = "自定义 API"

_ITEM_ARG = "_douyin_surf_item_id"
_PENDING_MAX_AGE_SECONDS = 10 * 60
_DOUYIN_EMPTY_PAGE_COOLDOWN_SECONDS = 3 * 60
# 手机号登录和图形验证可能需要较长时间；等待期间绝不能重启隐藏浏览器，
# 否则会关闭用户正在操作的可见窗口。
_DOUYIN_AUTHENTICATION_COOLDOWN_SECONDS = 15 * 60
# MaiBot 官方 NapCat 适配器公开的 QQ 群消息 API。
_NAPCAT_GROUP_MESSAGE_API = "adapter.napcat.group.send_group_msg"
# MaiBot 官方 SnowLuma 适配器公开的 QQ 群消息 API。
_SNOWLUMA_GROUP_MESSAGE_API = "adapter.napcat.message.send_group_msg"
_VIDEO_SENDER_AUTO = "自动识别"
_VIDEO_SENDER_NAPCAT = "NapCat"
_VIDEO_SENDER_SNOWLUMA = "SnowLuma"
_INTERNAL_SHARE_ANALYSIS_MARKERS = (
    "内容判断",
    "适合群聊转发",
    "值得分享的是",
    "不能证明",
    "不应把",
    "事实核验",
    "置信度",
    "理由：",
    "信息性质：",
)
_NON_PUBLIC_SHARE_OBSERVATION_MARKERS = (
    "不主动",
    "不建议",
    "不适合",
    "不入库",
    "质量不足",
    "证据不足",
    "无法核验",
    "年龄未确认",
    "待核",
    "风险",
    "合规",
    "无违规",
    "候选",
)


def _url_allowed_for_deep_browsing(url: str, allowed_domains: list[str]) -> bool:
    try:
        host = (urlsplit(str(url or "").strip()).hostname or "").lower().rstrip(".")
    except ValueError:
        return False
    if not host:
        return False
    return any(
        host == domain or host.endswith(f".{domain}")
        for raw_domain in allowed_domains
        if (domain := str(raw_domain or "").strip().lower().lstrip("."))
    )


def _text(value: Any) -> str:
    return str(value or "").strip()


def _video_sender_api_names(adapter: str) -> tuple[str, ...]:
    """按用户选择返回原生视频发送 API；自动模式依次尝试已安装的适配器。"""

    normalized = _text(adapter)
    if normalized == _VIDEO_SENDER_NAPCAT:
        return (_NAPCAT_GROUP_MESSAGE_API,)
    if normalized == _VIDEO_SENDER_SNOWLUMA:
        return (_SNOWLUMA_GROUP_MESSAGE_API,)
    return (_NAPCAT_GROUP_MESSAGE_API, _SNOWLUMA_GROUP_MESSAGE_API)


def _format_surf_result_message(result: dict[str, Any]) -> str:
    """给手动冲浪命令返回不误导用户的处理结果。"""
    results = int(result.get("results") or 0)
    new = int(result.get("new") or 0)
    if not bool(result.get("success")):
        error = _text(result.get("error"))
        lowered = error.lower()
        if "timeout" in lowered or "超时" in error:
            reason = "后台模型超时"
        elif "json" in lowered:
            reason = "后台模型返回格式错误"
        else:
            reason = "后台筛选调用失败"
        return (
            f"这轮抓到 {results} 条线索（新增 {new} 条），但{reason}；"
            "候选已经保留，稍后会换备用模型重试，并不是全部被筛掉了。"
        )

    evaluated = int(result.get("evaluated") or 0)
    kept = int(result.get("kept") or result.get("curated") or 0)
    return f"这轮抓到 {results} 条线索（新增 {new} 条），筛完 {evaluated} 条，留下 {kept} 条值得继续看的内容。"


def _should_reserve_mature_slot(total_shared: int, mature_shared: int, daily_limit: int, mature_quota: int) -> bool:
    """按目标比例穿插视觉内容，并在剩余名额不足时硬性保留配额。"""
    limit = max(0, int(daily_limit))
    quota = max(0, min(limit, int(mature_quota)))
    remaining_total = max(0, limit - max(0, int(total_shared)))
    remaining_mature = max(0, quota - max(0, int(mature_shared)))
    if not remaining_total or not remaining_mature:
        return False
    if remaining_mature >= remaining_total:
        return True
    return int(mature_shared) * limit <= int(total_shared) * quota


def _share_source_bucket(item: dict[str, Any]) -> str:
    """把同一平台的不同搜索方向归为一个分享来源，避免连续刷屏。"""

    source_text = " ".join(
        _text(item.get(key)) for key in ("source", "url")
    ).lower()
    for keyword, bucket in (
        ("douyin", "抖音"),
        ("抖音", "抖音"),
    ):
        if keyword in source_text:
            return bucket
    return _text(item.get("source"))


def _is_douyin_candidate(item: dict[str, Any]) -> bool:
    return _share_source_bucket(item) == "抖音"


def _is_douyin_note(item: dict[str, Any]) -> bool:
    """判断候选是否为抖音图文笔记。"""

    return "/note/" in _text(item.get("url")).lower()


def _manual_douyin_result_matches_query(item: dict[str, Any], query: str) -> bool:
    """在深读后确认手动点播候选确实属于请求标签。"""

    normalized_query = re.sub(r"\s+", "", _text(query)).lower()
    if not normalized_query:
        return False
    # ``snippet`` 会保留“搜索标签：<关键词>”等检索来源元数据，而 ``topic``、
    # ``summary`` 等模型归纳字段也可能直接复述这段元数据。它们都不能作为作品
    # 相关性的证据，否则任意搜索结果都可能因页面残留的关键词而误通过。只使用
    # 视频页原始标题和原始内容区文本。
    searchable_text = "".join(
        _text(item.get(key))
        for key in ("title", "observed_title", "full_text")
    )
    return normalized_query in re.sub(r"\s+", "", searchable_text).lower()


def _page_media_urls(images: Any, max_images: int) -> list[str]:
    """从深读页面的图片线索中保留正文大图，排除头像、图标和小组件。"""

    if not isinstance(images, list):
        return []
    urls: list[str] = []
    seen: set[str] = set()
    rejected_markers = ("avatar", "face", "logo", "icon", "emoji", "qrcode", "qr-code", "badge")
    for raw_image in images:
        if not isinstance(raw_image, dict):
            continue
        src = _text(raw_image.get("src"))
        if not src.startswith("https://") or src in seen:
            continue
        width = int(raw_image.get("width") or 0)
        height = int(raw_image.get("height") or 0)
        if width < 280 or height < 180:
            continue
        metadata = " ".join(_text(raw_image.get(key)) for key in ("src", "alt", "title")).lower()
        if any(marker in metadata for marker in rejected_markers):
            continue
        seen.add(src)
        urls.append(src)
        if len(urls) >= max(1, int(max_images)):
            break
    return urls


def _share_comment_is_relevant(response: str, item: dict[str, Any]) -> bool:
    """Cheaply reject replies that were generated for an unrelated active task."""

    raw_comment = _text(response).lower()
    raw_source = " ".join(
        _text(item.get(key))
        for key in ("title", "observed_title", "summary", "interesting_point", "stance", "full_text")
    ).lower()
    comment_words = set(re.findall(r"[a-z0-9]{3,}", raw_comment))
    source_words = set(re.findall(r"[a-z0-9]{3,}", raw_source))
    if comment_words & source_words:
        return True
    comment = "".join(re.findall(r"[\u4e00-\u9fff]", raw_comment))
    source = "".join(re.findall(r"[\u4e00-\u9fff]", raw_source))
    if len(comment) < 2 or len(source) < 2:
        return False
    common = {
        "这个", "那个", "就是", "真的", "还是", "不是", "可以", "已经", "感觉", "觉得",
        "内容", "一个", "一下", "这么", "怎么", "什么", "比较", "这种", "这次", "现在",
    }
    comment_bigrams = {comment[index : index + 2] for index in range(len(comment) - 1)} - common
    source_bigrams = {source[index : index + 2] for index in range(len(source) - 1)} - common
    comment_trigrams = {comment[index : index + 3] for index in range(len(comment) - 2)}
    source_trigrams = {source[index : index + 3] for index in range(len(source) - 2)}
    return bool(comment_trigrams & source_trigrams) or len(comment_bigrams & source_bigrams) >= 2


def _public_share_observation(item: dict[str, Any]) -> str:
    """从前序观察中挑出可直接对群友说的具体内容，不泄露筛选过程。"""

    for key in ("interesting_point", "share_intent", "summary", "stance"):
        raw_value = _text(item.get(key))
        if not raw_value:
            continue
        sentences = [
            re.sub(r"\s+", " ", sentence).strip(" ，,。；;：:")
            for sentence in re.split(r"[。！？!？\n]+", raw_value)
        ]
        for sentence in sentences:
            if len(sentence) < 6:
                continue
            if any(marker in sentence for marker in _NON_PUBLIC_SHARE_OBSERVATION_MARKERS):
                continue
            if _looks_like_internal_share_analysis(sentence):
                continue
            return _compact_share_excerpt(sentence, 88)
    return ""


def _fallback_share_comment(item: dict[str, Any]) -> str:
    """生成不会泄露内部评估措辞的短群聊兜底评论。"""

    # 抖音视频和图文的即时分享不等模型生成，以免下载完成后还要等待一次模型。
    # 但候选在筛选和深读阶段已经有具体观察。优先把其中可公开的一点带回群里，
    # 既不泄露内部评分与风险判断，也避免每条内容都被同一句通用模板覆盖。
    if _is_douyin_candidate(item):
        raw_title = _text(item.get("observed_title")) or _text(item.get("title"))
        title = re.sub(r"https?://\S+|\s+", " ", raw_title).strip(" ，,。.!！?？")
        observation = _public_share_observation(item)
        comment_seed = int(item.get("id") or 0) + sum(ord(char) for char in title)
        if observation:
            templates = (
                "我主要是被这点勾住：{observation}。",
                "这条有意思的地方在于：{observation}。",
                "比起标题，我更在意这点：{observation}。",
            )
            return templates[comment_seed % len(templates)].format(observation=observation)
        if title:
            title = title[:28].rstrip(" ，,。.!！?？")
            if len(raw_title) > len(title):
                title += "…"
            templates = (
                "「{title}」这个点还挺对胃口，顺手丢来。",
                "这条「{title}」我先放这儿，感觉会有人想接。",
                "「{title}」这个设定有点意思，拿来给你们看看。",
            )
            return templates[comment_seed % len(templates)].format(title=title)
        return "这条有个点还挺有意思，顺手丢群里一起看看。"

    source_text = " ".join(
        _text(item.get(key))
        for key in ("title", "observed_title", "summary", "interesting_point", "share_intent", "stance")
    )
    risk_label = _text(item.get("risk_label")).lower()
    if bool(item.get("official_today")) or risk_label == "official":
        return "这条刚出的官方内容还挺值得看，先丢群里一起瞅瞅。"
    if "评论区" in source_text or re.search(r"\d+\s*条?(?:评论|回复)", source_text):
        return "这帖评论区比正文还有节目效果，里面那些说法先当梗看就好。"
    if risk_label == "community":
        return "这帖还挺有节目效果，先丢群里一起看看，具体说法别急着当真。"
    return "这条还挺有意思，先丢群里一起看看。"


def _looks_like_internal_share_analysis(response: str) -> bool:
    """识别不应直接发送给群友的分析报告措辞。"""

    normalized = _text(response)
    return any(marker in normalized for marker in _INTERNAL_SHARE_ANALYSIS_MARKERS)


def _compact_share_excerpt(value: str, max_chars: int) -> str:
    """只保留适合聊天展示的摘要，完整正文仍留在本地存储。"""
    limit = max(80, min(800, int(max_chars)))
    lines = [re.sub(r"\s+", " ", line).strip() for line in _text(value).splitlines()]
    lines = [
        line
        for line in lines
        if line
        and not re.fullmatch(r"(?:首页|登录|注册|返回|分享|收藏|举报|加载更多)", line)
    ]
    text = " ".join(lines)
    if len(text) <= limit:
        return text
    clipped = text[: max(1, limit - 2)]
    sentence_end = max(clipped.rfind(marker) for marker in ("。", "！", "？", "!", "?"))
    if sentence_end >= max(40, limit // 2):
        clipped = clipped[: sentence_end + 1]
    return clipped.rstrip("，,；;：: ") + "……"


def _format_share_message(item: dict[str, Any], response: str, excerpt_max_chars: int) -> str:
    """将主动分享组织成自然转发，而不是新闻播报式长文。"""
    comment = _compact_share_excerpt(response, 180)
    url = _text(item.get("url"))
    # 抖音合并转发首节点已包含来源链接；后续简评只保留自然反应，避免同一
    # URL 在一条分享里重复两次。截图分享仍须保留链接以便群友打开原帖。
    if _is_douyin_candidate(item):
        return comment
    if _text(item.get("screenshot_base64")):
        return f"{comment}\n原帖：{url}"
    title = (_text(item.get("observed_title")) or _text(item.get("title")))[:120]
    excerpt = _compact_share_excerpt(_text(item.get("full_text")), excerpt_max_chars)
    return f"{comment}\n\n原帖摘要：{title}\n{excerpt}\n{url}"


def _format_manual_douyin_share_message(item: dict[str, Any], response: str) -> str:
    """手动抖音点播只发送短评，来源已写在合并转发首节点。"""

    comment = _compact_share_excerpt(response, 180)
    return comment


def _strip_forwarded_source(value: str) -> str:
    marker = "\n\n——\n分享："
    text = _text(value)
    if marker in text:
        return text.split(marker, 1)[1].strip()
    for suffix_marker in ("\n\n原帖摘要：", "\n原帖："):
        if suffix_marker in text:
            return text.split(suffix_marker, 1)[0].strip()
    return text


def _json_bool(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return value != 0
    return _text(value).lower() in {"1", "true", "yes", "on", "是", "有"}


def _score(value: Any, default: float = 0.0) -> float:
    try:
        return max(0.0, min(1.0, float(value)))
    except (TypeError, ValueError):
        return max(0.0, min(1.0, float(default)))


def _message_payload_text(value: Any) -> str:
    if isinstance(value, str):
        return value
    if isinstance(value, list):
        parts: list[str] = []
        for item in value:
            if isinstance(item, str):
                parts.append(item)
            elif isinstance(item, dict):
                content = item.get("text") or item.get("content")
                if isinstance(content, str):
                    parts.append(content)
        return " ".join(parts)
    if isinstance(value, dict):
        return _message_payload_text(value.get("text") or value.get("content"))
    return ""


def _context_item_text(item: Any) -> str:
    """Read text from both legacy messages and current Context Item payloads."""

    if not isinstance(item, dict):
        return ""
    legacy_text = _message_payload_text(item.get("content"))
    if legacy_text:
        return legacy_text
    return _message_payload_text(item.get("parts"))


def _is_user_context_item(item: Any) -> bool:
    if not isinstance(item, dict):
        return False
    return _text(item.get("role")).lower() == "user" or item.get("item_type") == "UserMessageItem"


def _is_douyin_surf_proactive_request(items: list[Any]) -> bool:
    """Ignore later internal user-role items when locating the active trigger."""

    for item in reversed(items):
        if not _is_user_context_item(item):
            continue
        content = _context_item_text(item).strip()
        if re.search(
            r"<plugin_proactive_task\b[^>]*\bplugin_id=[\"']chunian\.maibot-plugin-douyin-surf-lab[\"']",
            content,
            flags=re.IGNORECASE,
        ):
            return True
        # A real chat message newer than the task means the human turn won.
        # Reminders, recalled memory, time and Planner instructions deliberately
        # also use UserMessageItem, but none of them start with <message>.
        if re.match(r"^<message\b", content, flags=re.IGNORECASE):
            return False
    return False


def _planner_query_text(messages: list[Any], limit: int = 8) -> str:
    parts: list[str] = []
    for message in messages[-limit:]:
        content = _context_item_text(message)
        if content:
            parts.append(content)
    return "\n".join(parts)[-8000:]


def _message_plain_text(message: Any) -> str:
    if not isinstance(message, dict):
        return ""
    for key in ("plain_text", "processed_plain_text", "text"):
        value = message.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return _message_payload_text(message.get("raw_message"))


def _message_timestamp(message: Any) -> float:
    if not isinstance(message, dict):
        return 0.0
    try:
        return float(message.get("timestamp") or 0)
    except (TypeError, ValueError):
        return 0.0


def _parse_active_hours(value: str) -> tuple[datetime.time, datetime.time] | None:
    try:
        start_raw, end_raw = str(value or "").split("-", 1)
        return datetime.strptime(start_raw.strip(), "%H:%M").time(), datetime.strptime(end_raw.strip(), "%H:%M").time()
    except (TypeError, ValueError):
        return None


def _within_active_hours(now: datetime, value: str) -> bool:
    """判断当前时间是否落在左闭右开的工作时段内。

    例如 ``09:00-23:00`` 表示从 09:00:00 开始，到 22:59:59 结束；
    23:00:00 起不再浏览或分享。这样配置边界不会多运行一整分钟。
    """

    parsed = _parse_active_hours(value)
    if parsed is None:
        return False
    start, end = parsed
    current = now.time()
    if start <= end:
        return start <= current < end
    return current >= start or current < end


def _published_within_days(value: str, days: int, now: datetime) -> bool:
    """判断明确发布时间是否落在可选的最近天数范围内。"""

    text = _text(value)
    if not text:
        return False
    if any(token in text.lower() for token in ("今天", "刚刚", "小时前", "分钟前", "today")):
        return True
    try:
        published_at = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        try:
            published_at = datetime.strptime(text[:10], "%Y-%m-%d")
        except ValueError:
            return False
    if published_at.tzinfo is not None:
        published_at = published_at.astimezone().replace(tzinfo=None)
    return published_at >= now.replace(tzinfo=None) - timedelta(days=max(1, int(days)))


def _json_list(value: Any) -> list[str]:
    if isinstance(value, list):
        return [str(item) for item in value if str(item).strip()]
    if isinstance(value, str):
        try:
            parsed = json.loads(value)
        except (json.JSONDecodeError, TypeError):
            return []
        if isinstance(parsed, list):
            return [str(item) for item in parsed if str(item).strip()]
    return []


def _reply_style_context(reply_style: str) -> str:
    value = _text(reply_style)
    if not value:
        return ""
    return "【分享附言风格】\n" + value + "\n这是口吻约束，不得因此省略本轮 new_contribution。"


class DouyinSurfPlugin(MaiBotPlugin):
    config_model = DouyinSurfConfig

    def get_webui_config_schema(self, **kwargs: str) -> dict[str, Any]:
        """标记模型任务字段，由宿主在打开设置页时填入当前可用任务列表。"""

        schema = super().get_webui_config_schema(**kwargs)
        surf_fields = schema.get("sections", {}).get("surf", {}).get("fields", {})
        for field_name in ("curator_model", "vision_model"):
            field_schema = surf_fields.get(field_name)
            if isinstance(field_schema, dict):
                field_schema["dynamic_choices"] = "llm_tasks"
        return schema

    def _curator_model_task(self) -> str:
        return self.config.surf.curator_model.strip()

    def _vision_model_task(self) -> str:
        return self.config.surf.vision_model.strip()

    async def _generate_text_model(self, prompt: Any, temperature: float, max_tokens: int) -> dict[str, Any]:
        if self.config.surf.curator_model == _DIRECT_API_OPTION:
            return await generate_openai_compatible(self.config.direct_text_model, prompt=prompt, temperature=temperature, max_tokens=max_tokens)
        return await generate_background(self.ctx, prompt=prompt, model=self._curator_model_task(), temperature=temperature, max_tokens=max_tokens)

    async def _generate_vision_model(self, prompt: Any, temperature: float, max_tokens: int) -> dict[str, Any]:
        if self.config.surf.vision_model == _DIRECT_API_OPTION:
            return await generate_openai_compatible(self.config.direct_vision_model, prompt=prompt, temperature=temperature, max_tokens=max_tokens)
        return await self.ctx.call_capability("llm.generate", timeout_ms=180_000, model=self._vision_model_task(), temperature=temperature, max_tokens=max_tokens, prompt=prompt)

    async def _notify_missing_model_tasks(self) -> None:
        """提示使用者补齐当前配置实际引用的模型任务。"""

        result = await self.ctx.call_capability("llm.get_available_models")
        available_models = result.get("models") if isinstance(result, dict) else None
        if not isinstance(available_models, list):
            logger.warning("无法读取 MaiBot 已配置模型任务，稍后调用时会返回具体错误")
            return

        required_tasks = {
            "筛选主模型": self._curator_model_task(),
            "视觉理解模型": self._vision_model_task(),
        }
        missing = [f"{label}（{task}）" for label, task in required_tasks.items() if task and task not in available_models]
        if not missing:
            return

        notice = "⚠️ 抖音冲浪插件缺少模型配置：" + "、".join(missing) + "。请到 MaiBot 的模型管理中配置对应任务，或在插件设置里改为已有任务。"
        logger.warning(notice)
        for stream in await self._resolve_allowed_streams():
            stream_id = _text(stream.get("session_id") or stream.get("stream_id"))
            if stream_id:
                await self.ctx.send.text(notice, stream_id)

    async def on_load(self) -> None:
        # 采用独立数据库，避免与其他插件的候选和分享记录相互影响。
        self._store = LifeStore(self.ctx.paths.data_dir / "douyin_surf.sqlite3")
        self._scheduler_task: asyncio.Task[None] | None = None
        self._background_tasks: set[asyncio.Task[Any]] = set()
        self._pending_share: dict[str, tuple[int, float]] = {}
        self._active_proactive_share_sessions: set[str] = set()
        self._pending_screenshot_sends: set[tuple[int, str]] = set()
        self._planner_share_context: dict[str, str] = {}
        self._inventory_replenish_task: asyncio.Task[None] | None = None
        self._surf_lock = asyncio.Lock()
        self._observation_lock = asyncio.Lock()
        # 冲浪筛选和深读共用一把锁，避免后台模型请求堆积。
        self._background_llm_lock = asyncio.Lock()
        self._browser = DeepBrowser(
            self.ctx.paths.data_dir / "browser-profile",
            list(self.config.browser.allowed_domains),
            self.config.browser.page_timeout_seconds,
        )
        await self._notify_missing_model_tasks()
        self._start_scheduler()
        logger.info("抖音冲浪与分享已加载：自动推荐流和 /抖音 搜索已独立运行")

    async def on_unload(self) -> None:
        if self._scheduler_task is not None:
            self._scheduler_task.cancel()
            try:
                await self._scheduler_task
            except asyncio.CancelledError:
                pass
            self._scheduler_task = None
        for task in list(self._background_tasks):
            task.cancel()
        if self._background_tasks:
            await asyncio.gather(*self._background_tasks, return_exceptions=True)
        self._background_tasks.clear()
        await self._browser.close()

    async def on_config_update(self, scope: str, config_data: dict[str, object], version: str) -> None:
        del config_data, version
        if scope == "self":
            self._start_scheduler()

    def _enabled(self) -> bool:
        return bool(self.config and self.config.plugin.enabled)

    async def _send_command_reply(self, stream_id: str, text: str) -> tuple[bool, str, bool]:
        """Command return text is log-only in the current host, so send explicitly."""
        clean_stream = _text(stream_id)
        clean_text = _text(text)
        if not clean_stream:
            logger.warning("命令回复缺少 stream_id: %s", clean_text[:120])
            return False, clean_text, True
        sent = await self.ctx.send.text(clean_text, clean_stream)
        if not sent:
            logger.warning("命令回复发送失败 stream=%s text=%s", clean_stream, clean_text[:120])
            return False, clean_text, True
        return True, clean_text, True

    @Command(
        "douyin_surf_help",
        description="查看抖音冲浪与分享插件的命令",
        pattern=r"^/(?:抖音帮助|douyin_help)$",
    )
    async def command_help(self, stream_id: str = "", **kwargs: Any):
        del kwargs
        help_text = (
            "抖音冲浪与分享｜帮助\n"
            "\n"
            "【搜索】\n"
            "/抖音 <关键词>：从抖音综合搜索结果中按点赞优先挑选并转发一条\n"
            "\n"
            "【浏览器登录】\n"
            "/抖音浏览器登录 [URL]：打开本插件独立的 Chrome 档案；不带 URL 时直达抖音\n"
            "\n"
            "【自动运行】\n"
            "插件按配置浏览已登录的抖音推荐流，只有候选达到点赞、质量和时段限制后才会在白名单群分享。"
        )
        return await self._send_command_reply(stream_id, help_text)

    def _start_scheduler(self) -> None:
        if self._scheduler_task is not None and not self._scheduler_task.done():
            return
        self._scheduler_task = asyncio.create_task(self._scheduler_loop())

    def _track_task(self, coroutine: Any) -> None:
        task = asyncio.create_task(coroutine)
        self._background_tasks.add(task)
        task.add_done_callback(self._finish_background_task)

    def _start_inventory_replenishment(self) -> None:
        """启动唯一的候选库存补货任务，避免定时轮次重叠打开浏览器。"""

        task = self._inventory_replenish_task
        if task is not None and not task.done():
            return
        task = asyncio.create_task(self._replenish_candidate_inventory())
        self._inventory_replenish_task = task
        self._background_tasks.add(task)
        task.add_done_callback(self._finish_inventory_replenishment)

    def _finish_inventory_replenishment(self, task: asyncio.Task[Any]) -> None:
        self._finish_background_task(task)
        if self._inventory_replenish_task is task:
            self._inventory_replenish_task = None

    async def _replenish_candidate_inventory(self) -> None:
        """任一聊天流候选到低水位时连续刷推荐流，直到各自补到自己的上限。"""

        pause_seconds = max(1, int(self.config.surf.replenish_cycle_pause_seconds))
        logger.info("分聊天流候选库存补货任务已启动")
        try:
            while (
                self._enabled()
                and self.config.surf.enabled
                and _within_active_hours(datetime.now(), self.config.surf.active_hours)
            ):
                # 深读完成前，候选尚不能计入聊天流库存。继续抓取只会让初筛队列
                # 无限积压，反而延后第一条实际可分享内容的产生。
                pending_observations = self._store.pending_observation_count()
                if pending_observations:
                    logger.info(
                        "候选深读待处理，暂停继续补货 pending=%s",
                        pending_observations,
                    )
                    return
                inventories = await self._stream_candidate_inventories()
                if not inventories:
                    logger.info("没有启用的分聊天流分享规则，停止候选补货")
                    return
                if all(inventory >= rule.candidate_inventory_pause_at for _, rule, inventory in inventories):
                    logger.info("分聊天流候选库存补货完成 inventories=%s", [(stream_id, inventory) for stream_id, _, inventory in inventories])
                    return
                result = await self._run_surf_cycle(
                    discover=True,
                    inventory_replenishment=True,
                )
                if not bool(result.get("success")):
                    logger.warning("候选库存补货轮次未完成 reason=%s", _text(result.get("reason")))
                await asyncio.sleep(pause_seconds)
        except asyncio.CancelledError:
            raise
        finally:
            # 库存达到上限、插件停用或任务被取消时，统一关闭复用中的推荐页，
            # 不留下搜索页、空白页或后台浏览器进程。
            await self._browser.close_douyin_recommendations()

    def _finish_background_task(self, task: asyncio.Task[Any]) -> None:
        self._background_tasks.discard(task)
        if task.cancelled():
            return
        try:
            exc = task.exception()
            if exc is not None:
                logger.error("抖音冲浪后台任务异常", exc_info=(type(exc), exc, exc.__traceback__))
        except Exception:
            logger.exception("抖音冲浪后台任务异常")

    async def _scheduler_loop(self) -> None:
        startup_delay = max(10, int(self.config.surf.startup_delay_seconds)) if self.config else 180
        try:
            await asyncio.sleep(startup_delay)
            while True:
                if self._enabled():
                    try:
                        await self._scheduled_tick()
                    except Exception:
                        logger.exception("抖音冲浪定时任务失败")
                await asyncio.sleep(60)
        except asyncio.CancelledError:
            return

    async def _scheduled_tick(self) -> None:
        now = time.time()
        recovered_queued = self._store.recover_stale_queued_shares(_PENDING_MAX_AGE_SECONDS)
        if recovered_queued:
            logger.info("已恢复未完成的冲浪分享队列 count=%s", recovered_queued)
        self._prune_retained_data(now)
        # 先消费已经完成深读的候选。搜索、浏览和模型筛选可能持续数分钟，
        # 若把分享放在它们之后，会让 15 分钟的发送节奏被后台冲浪长期饿死。
        if self.config.sharing.enabled:
            await self._maybe_trigger_share()
        has_pending_curation = bool(self._store.pending_curation(1))
        retry_after = float(self._store.get_state("curation_retry_after", "0") or 0)
        next_batch_at = float(self._store.get_state("curation_next_batch_at", "0") or 0)
        if self.config.surf.enabled and _within_active_hours(datetime.now(), self.config.surf.active_hours):
            inventories = await self._stream_candidate_inventories()
            low_inventory_groups = [
                (stream_id, inventory, rule.candidate_inventory_resume_below, rule.candidate_inventory_pause_at)
                for stream_id, rule, inventory in inventories
                if inventory <= rule.candidate_inventory_resume_below
            ]
            pending_observations = self._store.pending_observation_count()
            if low_inventory_groups and not pending_observations:
                logger.info("分聊天流候选库存触及补货水位 streams=%s", low_inventory_groups)
                self._start_inventory_replenishment()

            curation_due = has_pending_curation and now >= max(retry_after, next_batch_at)
            if curation_due and not self._surf_lock.locked():
                await self._run_surf_cycle(discover=False)
            if not self._observation_lock.locked() and self._store.pending_observation(1):
                self._track_task(self._observe_next_candidate())

    def _prune_retained_data(self, now: float) -> None:
        """每天只执行一次候选与分享记录清理，避免数据库无限累积。"""

        if not self.config.retention.enabled:
            return
        today = datetime.now().astimezone().date().isoformat()
        if self._store.get_state("retention_last_cleanup_date", "") == today:
            return
        retention = self.config.retention
        removed = self._store.prune_retained_data(
            ordinary_candidate_days=retention.ordinary_candidate_days,
            dismissed_days=retention.dismissed_days,
            shared_days=retention.shared_days,
            now=now,
        )
        self._store.set_state("retention_last_cleanup_date", today)
        if any(removed.values()):
            logger.info("冲浪记录自动清理完成 removed=%s", removed)

    async def _discover_native_direction(
        self,
        source: str,
        query: str,
        *,
        douyin_keyword_count: int | None = None,
        keep_douyin_recommendation_open: bool = False,
    ) -> list[dict[str, Any]]:
        """Route a configured surf direction through the site's own pages."""
        max_results = self.config.surf.search_results_per_query
        headless = self.config.browser.headless
        scroll_rounds = 4
        if source == "美图·抖音推荐":
            authentication_state_key = f"auto_douyin_authentication_until:{source}"
            authentication_retry_after = float(self._store.get_state(authentication_state_key, "0") or 0)
            if authentication_retry_after > time.time():
                remaining_seconds = max(1, int(authentication_retry_after - time.time()))
                if await self._browser.douyin_authentication_pending():
                    logger.info(
                        "抖音推荐流等待人工完成安全验证，暂不重复打开 retry_after=%ss",
                        remaining_seconds,
                    )
                    return []
                self._store.set_state(authentication_state_key, 0)
                logger.info("检测到抖音登录或验证已完成，立即恢复推荐流冲浪")
            try:
                return await self._browser.discover_douyin_recommendations(
                    max_results=self.config.browser.douyin_recommendation_candidates_per_cycle,
                    cards_to_browse=self.config.browser.douyin_recommendation_cards_per_cycle,
                    headless=self.config.browser.headless,
                    min_like_count=self.config.candidate_filter.min_like_count,
                    min_comment_count=self.config.candidate_filter.min_comment_count,
                    min_collect_count=self.config.candidate_filter.min_collect_count,
                    min_share_count=self.config.candidate_filter.min_share_count,
                    allow_douyin_notes=self.config.candidate_filter.allow_douyin_notes,
                    max_video_duration_seconds=self.config.candidate_filter.max_video_duration_seconds,
                    # 推荐流与过去的搜索结果共用去重池，已经处理过的作品不再占用
                    # 刷推荐的名额，才能持续向后补足本地候选库存。
                    excluded_urls=self._store.known_candidate_urls(),
                    keep_open=keep_douyin_recommendation_open,
                )
            except DouyinSearchAuthenticationError as exc:
                self._store.set_state(
                    authentication_state_key,
                    time.time() + _DOUYIN_AUTHENTICATION_COOLDOWN_SECONDS,
                )
                await self._request_douyin_authentication(reason=str(exc), url=exc.url)
                logger.warning(
                    "抖音推荐流出现人工安全验证，暂停自动冲浪 %s 秒 reason=%s",
                    _DOUYIN_AUTHENTICATION_COOLDOWN_SECONDS,
                    exc,
                )
                return []
        # 自动冲浪传入的 query 就是当前要补货的标签。此前遗漏了这一步，
        # 导致标签搜索虽然被选中，却没有真正提交给抖音综合页。
        targets = [_text(query)] if _text(query) else []
        if targets and source.startswith("美图·"):
            # 同一轮关键词共用一个游标，避免连续重复搜索同一个标签。
            cursor_key = "douyin_target_cursor"
            try:
                cursor = int(self._store.get_state(cursor_key, "0") or 0)
            except (TypeError, ValueError):
                cursor = 0
            target_count = 1
            if "抖音" in source:
                target_count = min(
                    len(targets),
                    max(
                        1,
                        int(
                            douyin_keyword_count
                            if douyin_keyword_count is not None
                            else self.config.browser.douyin_keywords_per_cycle
                        ),
                    ),
                )
            targets = [targets[(cursor + offset) % len(targets)] for offset in range(target_count)]
            self._store.set_state(cursor_key, cursor + target_count)
            logger.info("抖音冲浪关键词 source=%s keywords=%s", source, targets)

        if not targets:
            return []
        if "抖音" in source:
            attempts = max(0, int(self.config.browser.douyin_search_retry_count)) + 1
            douyin_headless = self.config.browser.headless
            authentication_state_key = f"auto_douyin_authentication_until:{source}"
            authentication_retry_after = float(self._store.get_state(authentication_state_key, "0") or 0)
            if authentication_retry_after > time.time():
                remaining_seconds = max(1, int(authentication_retry_after - time.time()))
                if await self._browser.douyin_authentication_pending():
                    logger.info(
                        "抖音等待人工完成安全验证，暂不重复打开搜索页 source=%s retry_after=%ss",
                        source,
                        remaining_seconds,
                    )
                    return []
                self._store.set_state(authentication_state_key, 0)
                logger.info("检测到抖音登录或验证已完成，立即恢复搜索冲浪 source=%s", source)
            # 已经入库、发过或被筛掉的作品都不应反复占据综合页首屏。把它们传给
            # 页面解析器后，首屏凑不齐新的两个候选时才会执行配置的一次下拉，
            # 从而在同一标签中持续向后寻找新内容。
            known_douyin_urls = self._store.known_candidate_urls(source)
            results: list[dict[str, Any]] = []
            failures: list[str] = []
            for target in targets:
                empty_page_state_key = f"auto_douyin_empty_page_until:{source}:{target}"
                empty_page_retry_after = float(self._store.get_state(empty_page_state_key, "0") or 0)
                if empty_page_retry_after > time.time():
                    remaining_seconds = max(1, int(empty_page_retry_after - time.time()))
                    logger.info(
                        "抖音综合页近期持续骨架屏，暂不重复打开 source=%s keyword=%s retry_after=%ss",
                        source,
                        target,
                        remaining_seconds,
                    )
                    failures.append(f"{target}: 综合页骨架屏冷却中")
                    continue
                for attempt in range(attempts):
                    try:
                        results.extend(
                            await self._browser.discover_douyin_search(
                                keyword=target,
                                # 自动冲浪只浏览综合页：它的首屏更符合本群的审美方向，
                                # 缺货时由库存补货循环继续换词搜索，而不混入视频页结果。
                                search_type="general",
                                max_results=max_results,
                                scroll_rounds=self.config.browser.auto_douyin_scroll_rounds,
                                headless=douyin_headless,
                                minimum_results_before_return=self.config.browser.auto_douyin_min_results_before_scroll,
                                min_like_count=self.config.candidate_filter.min_like_count,
                                min_comment_count=self.config.candidate_filter.min_comment_count,
                                min_collect_count=self.config.candidate_filter.min_collect_count,
                                min_share_count=self.config.candidate_filter.min_share_count,
                                allow_douyin_notes=self.config.candidate_filter.allow_douyin_notes,
                                max_video_duration_seconds=self.config.candidate_filter.max_video_duration_seconds,
                                excluded_urls=known_douyin_urls,
                            )
                        )
                        self._store.set_state(empty_page_state_key, 0)
                        break
                    except DouyinSearchNoResultError as exc:
                        # “有作品但不合格”会带有作品链接或解析数据，仍可按原设置
                        # 重试；截图中的永久骨架屏则没有任何作品链接，立即冷却该词，
                        # 不能让库存循环反复弹出同一张空白搜索页。
                        if "候选链接=0" in str(exc):
                            self._store.set_state(
                                empty_page_state_key,
                                time.time() + _DOUYIN_EMPTY_PAGE_COOLDOWN_SECONDS,
                            )
                            failures.append(f"{target}: 抖音综合页骨架屏")
                            logger.warning(
                                "抖音综合页未加载出作品，暂停该关键词 %s 秒 source=%s keyword=%s",
                                _DOUYIN_EMPTY_PAGE_COOLDOWN_SECONDS,
                                source,
                                target,
                            )
                            break
                        if attempt + 1 >= attempts:
                            failures.append(f"{target}: {exc}")
                            break
                        logger.info(
                            "抖音搜索首轮未得到候选，正在重试 source=%s keyword=%s reason=%s",
                            source,
                            target,
                            exc,
                        )
                        await asyncio.sleep(1.2)
                    except DouyinSearchAuthenticationError as exc:
                        self._store.set_state(
                            authentication_state_key,
                            time.time() + _DOUYIN_AUTHENTICATION_COOLDOWN_SECONDS,
                        )
                        await self._request_douyin_authentication(reason=str(exc), url=exc.url)
                        logger.warning(
                            "抖音搜索出现人工安全验证，暂停自动冲浪 %s 秒 source=%s keyword=%s reason=%s",
                            _DOUYIN_AUTHENTICATION_COOLDOWN_SECONDS,
                            source,
                            target,
                            exc,
                        )
                        return []
            if results:
                return results
            raise DouyinSearchNoResultError("；".join(failures) or "抖音搜索没有返回候选")
        return []

    async def _visually_screen_douyin_recommendations(
        self, items: list[dict[str, Any]], *, configured_tags: list[str]
    ) -> list[dict[str, Any]]:
        """按已配置标签语义初筛推荐流中实际出现的作品。"""

        normalized_tags = [_text(tag) for tag in configured_tags if _text(tag)]
        if not normalized_tags:
            return []
        approved: list[dict[str, Any]] = []
        for item in items:
            url = _text(item.get("url"))
            if not url:
                continue
            try:
                # 推荐流已经逐条停留在作品本身，并在确认标题对应后截取当前页面。
                # 直接使用该画面，不再额外打开 /video/... 详情页，避免打断自然刷流。
                image_base64 = _text(item.get("visual_screenshot_base64"))
                if not image_base64:
                    logger.info("推荐作品未截到当前页画面，跳过 VLM 审核 url=%s", url)
                    continue
                result = await self._generate_vision_model(
                    max_tokens=900,
                    temperature=0.1,
                    prompt=[
                        {
                            "role": "user",
                            "content": [
                                {
                                    "type": "text",
                                    "text": (
                                        "你是抖音推荐流的视觉初筛。只根据这一帧实际画面判断，不要从标题猜测。"
                                        f"可选标签：{json.dumps(normalized_tags, ensure_ascii=False)}。"
                                        "仅当画面清晰、与至少一个可选标签语义相关、且适合普通群聊分享时 candidate=true。"
                                        "广告、直播带货、露骨色情、暴力、隐私泄露或无法判断的画面一律 false。"
                                        "tags 只能填写可选标签中的原文；无匹配标签时 tags 必须为空。"
                                        "严格只输出 JSON："
                                        '{"candidate":false,"description":"不超过80字的可见画面",'
                                        '"tags":["内容标签"],"risk":"none/unsafe/unknown"}'
                                    ),
                                },
                                {"type": "image", "image_format": "jpeg", "image_base64": image_base64},
                            ],
                        }
                    ],
                )
                parsed = parse_json_object(llm_text(result)) if isinstance(result, dict) and result.get("success") else {}
                if not bool(parsed.get("candidate")) or _text(parsed.get("risk")) != "none":
                    logger.info("VLM 未通过推荐作品 url=%s risk=%s", url, _text(parsed.get("risk")))
                    continue
                tags = [
                    tag
                    for raw_tag in parsed.get("tags", [])
                    if (tag := _text(raw_tag)) in normalized_tags
                ]
                if not tags:
                    logger.info("推荐作品未命中已配置标签 url=%s", url)
                    continue
                enriched = dict(item)
                visual_evidence = f"视觉识别：{_text(parsed.get('description'))}；匹配标签：{'、'.join(tags)}"
                # 候选库的初始字段只有 snippet，因此把 VLM 证据放入 body，供
                # 将可见画面证据交给后续文本筛选器复核。
                enriched["source"] = "抖音推荐"
                enriched["body"] = f"{_text(item.get('body'))}\n{visual_evidence}".strip()
                enriched["summary"] = visual_evidence
                approved.append(enriched)
                logger.info("VLM 通过推荐作品，等待 background 最终审核 url=%s", url)
            except Exception as exc:
                logger.warning("推荐作品视觉审核失败 url=%s error=%s", url, exc)
        return approved

    async def _run_surf_cycle(
        self,
        *,
        manual: bool = False,
        discover: bool = True,
        inventory_replenishment: bool = False,
    ) -> dict[str, Any]:
        if not self._enabled() or not self.config.surf.enabled:
            return {"success": False, "reason": "自主冲浪未启用"}
        if self._surf_lock.locked():
            if not manual:
                return {"success": False, "reason": "已有冲浪任务正在运行"}
            # 手动命令不能被启动阶段或定时任务的“只筛库存”轮次吞掉。
            # 等待当前轮次释放锁后，仍要实际抓取新的搜索方向。
            logger.info("手动冲浪正在等待当前后台轮次结束，随后强制抓取新方向")

        async with self._surf_lock:
            # 搜索词直接由所有已启用聊天流的标签汇总而来，用户只维护一处标签。
            queries = [f"抖音|{tag}" for tag in self._all_configured_tags()]
            if not queries:
                return {"success": False, "reason": "请先添加至少一个启用的群聊或私聊规则，并填写标签"}
            selected: list[str] = []
            raw_results: list[dict[str, Any]] = []
            last_scan = float(self._store.get_state("last_scan_at", "0") or 0)
            scan_interval = max(5, int(self.config.surf.interval_minutes)) * 60
            # 手动点播沿用自己的即时搜索。库存补货会连续轮换配置关键词，
            # 不受旧的分钟间隔限制；其他内部轮次只处理已积压的候选。
            if discover and (manual or inventory_replenishment or time.time() - last_scan >= scan_interval):
                selected_count = min(
                    1 if inventory_replenishment else max(1, int(self.config.surf.directions_per_cycle)),
                    len(queries),
                )
                selected = select_surf_queries(
                    queries,
                    selected_count,
                )
                self._store.set_state("last_scan_at", time.time())
                douyin_slot_indexes = [
                    index
                    for index, raw_query in enumerate(selected)
                    if "抖音" in split_source_query(raw_query)[0]
                ]
                douyin_keyword_counts: dict[int, int] = {}
                if douyin_slot_indexes:
                    total_keywords = max(1, int(self.config.browser.douyin_keywords_per_cycle))
                    base_count, extra_count = divmod(total_keywords, len(douyin_slot_indexes))
                    for position, index in enumerate(douyin_slot_indexes):
                        douyin_keyword_counts[index] = base_count + (1 if position < extra_count else 0)

                for index, raw_query in enumerate(selected):
                    source, query = split_source_query(raw_query)
                    try:
                        direction_results: list[dict[str, Any]] = []
                        if self.config.browser.native_site_browsing_enabled:
                            try:
                                direction_results = await self._discover_native_direction(
                                    source,
                                    query,
                                    douyin_keyword_count=douyin_keyword_counts.get(index),
                                    keep_douyin_recommendation_open=inventory_replenishment,
                                )
                                for item in direction_results:
                                    item["source"] = source
                                if direction_results:
                                    logger.info("站内冲浪完成 source=%s results=%s", source, len(direction_results))
                            except Exception as exc:
                                logger.warning("抖音站内冲浪失败 source=%s error=%s", source, exc)
                        raw_results.extend(direction_results)
                    except Exception as exc:
                        logger.warning("社区搜索失败 source=%s query=%s error=%s", source, query, exc)

                # 搜索页保证当前低库存标签有稳定来源；推荐流则提供账号画像以外的
                # 新内容。推荐作品必须先由视觉模型归入已配置标签，才会进入候选库。
                if not manual:
                    try:
                        recommendation_results = await self._browser.discover_douyin_recommendations(
                            max_results=self.config.browser.douyin_recommendation_candidates_per_cycle,
                            cards_to_browse=self.config.browser.douyin_recommendation_cards_per_cycle,
                            headless=self.config.browser.headless,
                            min_like_count=self.config.candidate_filter.min_like_count,
                            min_comment_count=self.config.candidate_filter.min_comment_count,
                            min_collect_count=self.config.candidate_filter.min_collect_count,
                            min_share_count=self.config.candidate_filter.min_share_count,
                            allow_douyin_notes=self.config.candidate_filter.allow_douyin_notes,
                            max_video_duration_seconds=self.config.candidate_filter.max_video_duration_seconds,
                            excluded_urls=self._store.known_candidate_urls(),
                            keep_open=inventory_replenishment,
                        )
                        raw_results.extend(
                            await self._visually_screen_douyin_recommendations(
                                recommendation_results,
                                configured_tags=self._all_configured_tags(),
                            )
                        )
                    except DouyinSearchAuthenticationError as exc:
                        await self._request_douyin_authentication(reason=str(exc), url=exc.url)
                        logger.warning("抖音推荐流等待人工完成安全验证，本轮仅保留搜索结果：%s", exc)
                    except Exception as exc:
                        logger.warning("抖音推荐流浏览失败，本轮仅保留搜索结果：%s", exc)

            allowed_domains = list(self.config.browser.allowed_domains)
            allowed_results = [
                item
                for item in raw_results
                if _url_allowed_for_deep_browsing(
                    _text(item.get("url") or item.get("href")),
                    allowed_domains,
                )
                and not is_official_url(_text(item.get("url") or item.get("href")))
            ]
            blocked_count = len(raw_results) - len(allowed_results)
            if blocked_count:
                logger.info("已跳过白名单外或官方来源候选 count=%s", blocked_count)
            candidate_filter = self.config.candidate_filter
            eligible_results = allowed_results
            if candidate_filter.recent_only_enabled:
                eligible_results = [
                    item
                    for item in allowed_results
                    if _published_within_days(
                        _text(item.get("published_at") or item.get("date")),
                        candidate_filter.recent_days,
                        datetime.now(),
                    )
                ]
                stale_count = len(allowed_results) - len(eligible_results)
                if stale_count:
                    logger.info(
                        "已跳过超过最近天数或无日期候选 count=%s days=%s",
                        stale_count,
                        candidate_filter.recent_days,
                    )
            inserted_ids = self._store.add_candidates(eligible_results)
            pending = self._store.pending_curation(self.config.surf.max_candidates_per_batch)
            candidate_ids = [int(item["id"]) for item in pending]
            curated: list[dict[str, Any]] = []
            curation_error = ""
            if candidate_ids:
                try:
                    async with self._background_llm_lock:
                        curated = await curate_candidates(
                            self.ctx,
                            self._store,
                            candidate_ids,
                            values=self.config.identity.values,
                            topics=self._all_configured_tags(),
            model=self._curator_model_task(),
            generator=self._generate_text_model,
                        )
                except Exception as exc:
                    retry_seconds = max(1, int(self.config.surf.retry_backoff_minutes)) * 60
                    self._store.set_state("curation_retry_after", time.time() + retry_seconds)
                    logger.warning("冲浪筛选失败，%s 分钟后再试；本轮不会继续抓取新结果", retry_seconds // 60)
                    curation_error = str(exc)
                else:
                    self._store.set_state("curation_retry_after", 0)
                    batch_cooldown = max(0, int(self.config.surf.batch_cooldown_minutes)) * 60
                    self._store.set_state("curation_next_batch_at", time.time() + batch_cooldown)
            kept_count = sum(1 for item in curated if bool(item.get("keep")))
            logger.info(
                "自主冲浪完成 manual=%s sources=%s results=%s new=%s evaluated=%s kept=%s",
                manual,
                [split_source_query(item)[0] for item in selected],
                len(raw_results),
                len(inserted_ids),
                len(curated),
                kept_count,
            )
            return {
                "success": not bool(curation_error),
                "sources": [split_source_query(item)[0] for item in selected],
                "results": len(raw_results),
                "new": len(inserted_ids),
                "evaluated": len(curated),
                "kept": kept_count,
                "curated": kept_count,
                "error": curation_error,
            }

    async def _observe_next_candidate(self) -> None:
        """串行深读一小批候选，避免每分钟只消化一条而长期堵塞分享。"""

        async with self._observation_lock:
            # 浏览器和模型仍串行使用，避免并发页面、模型请求抢占同一登录档案。
            # 一轮处理三条可显著缩短候选到分享之间的等待，又不会造成突发高负载。
            pending = self._store.pending_observation(3)
            if not pending:
                return
            for discovery in pending:
                await self._deep_observe_discovery(discovery, discovery)

            # 深读结束后立即检查是否产生了可分享候选，避免再额外等待下一次定时 tick。
            if self.config.sharing.enabled:
                await self._maybe_trigger_share()

    async def _deep_observe_discovery(
        self,
        discovery: dict[str, Any],
        curated: dict[str, Any],
        *,
        manual_douyin: bool = False,
    ) -> None:
        url = _text(discovery.get("url"))
        discovery_id = int(discovery.get("id") or 0)
        if not url:
            return
        if discovery_id and not _url_allowed_for_deep_browsing(url, list(self.config.browser.allowed_domains)):
            self._store.dismiss_discovery(discovery_id, "页面不在深度浏览域名白名单中")
            logger.info("已跳过并移出白名单外候选 url=%s", url)
            return
        try:
            page: dict[str, Any] = {}
            is_douyin_video = "douyin.com/video" in url
            if is_douyin_video:
                browser_cookies = await self._browser.cookies_for(url)
                browser_headers = await self._browser.request_headers_for(url)
                duration = await probe_video_duration(
                    url,
                    self.ctx.paths.data_dir / "video-duration-probe-cache",
                    browser_cookies=browser_cookies,
                    browser_headers=browser_headers,
                )
                max_duration = self.config.candidate_filter.max_video_duration_seconds
                if duration <= 0 or duration > max_duration:
                    self._store.dismiss_discovery(
                        discovery_id,
                        f"视频时长 {duration or '未知'} 秒，不符合候选上限 {max_duration} 秒",
                    )
                    logger.info(
                        "已在候选阶段跳过超长或时长未知的抖音视频 item=%s duration=%s max_duration=%s",
                        discovery_id,
                        duration,
                        max_duration,
                    )
                    return
            use_video_observer = self.config.video.enabled and is_douyin_video and not self.config.video.douyin_browser_first
            if use_video_observer:
                observed_result = await self._observe_video_url(url)
                video = observed_result.get("video") if isinstance(observed_result, dict) else {}
                analysis = observed_result.get("analysis") if isinstance(observed_result, dict) else {}
                video = video if isinstance(video, dict) else {}
                parsed = dict(analysis) if isinstance(analysis, dict) else {}
                source_text = "\n\n".join(
                    item for item in (
                        _text(video.get("description")),
                        _text(video.get("subtitle")),
                    ) if item
                )
                if not source_text:
                    visual_observations = analysis.get("visual_observations") if isinstance(analysis, dict) else []
                    visual_text = "\n".join(
                        _text(item) for item in visual_observations if _text(item)
                    ) if isinstance(visual_observations, list) else ""
                    source_text = "\n\n".join(
                        item for item in (_text(analysis.get("summary")), visual_text) if item
                    )
                observed_title = _text(video.get("title")) or _text(discovery.get("title"))
                parsed.setdefault("forward_text", source_text)
            else:
                if not self.config.browser.enabled:
                    self._store.mark_observation_failed(discovery_id)
                    return
                page = await self._browser.read_page(
                    url,
                    headless=self.config.browser.headless,
                    max_chars=self.config.browser.max_text_chars,
                )
                observed_title = _text(page.get("title")) or _text(discovery.get("title"))
                source_text = _text(page.get("text"))
                prompt = (
                    "你是抖音内容核验器。只根据页面正文、评论和元信息判断，不补充事实。"
                    "输出单个 JSON：{\"summary\":\"不超过120字\",\"reasons\":[\"理由\"],\"confidence\":0.7,"
                    "\"share_score\":0.8,\"share_worthy\":true,\"content_quality_score\":0.8,"
                    "\"heat_score\":0.5,\"risk_label\":\"community\",\"share_intent\":\"分享角度\","
                    "\"screenshot_worthy\":false,\"screenshot_kind\":\"none\",\"screenshot_keyword\":\"\",\"unsafe\":false}。"
                    "拒绝广告、引流、标题党、不安全或无关内容；内容质量不足时 share_worthy=false。只输出 JSON。\n\n"
                    f"本地日期：{datetime.now().astimezone().date().isoformat()}\n"
                    f"来源方向：{discovery.get('source', '')}\n标题：{observed_title}\nURL：{page.get('url', url)}\n"
                    f"搜索阶段初步判断：{curated.get('stance', '')}\n"
                    f"页面可见图片线索：{json.dumps(page.get('images', []), ensure_ascii=False)}\n"
                    f"可选评论原文：{json.dumps(page.get('comments', []), ensure_ascii=False)}\n"
                    f"正文：\n{source_text}"
                )
                async with self._background_llm_lock:
                    result = await self._generate_text_model(prompt, 0.25, 1200 if manual_douyin else 2200)
                parsed = parse_json_object(llm_text(result)) or {}

            full_text = source_text
            if not full_text:
                self._store.mark_observation_failed(discovery_id)
                return
            if manual_douyin:
                # 手动 /抖音 只要求内容可用，仍由模型的 unsafe 字段执行安全拒绝。
                if _json_bool(parsed.get("unsafe")):
                    self._store.delete_discovery(discovery_id)
                    logger.info("已移除手动抖音搜索中的不安全候选 item=%s", discovery_id)
                    return
                gate = {
                    "share_score": _score(parsed.get("share_score"), _score(curated.get("share_score"), 0.0)),
                    "heat_score": _score(parsed.get("heat_score"), _score(curated.get("heat_score"), 0.0)),
                    "official_today": False,
                    "share_eligible": True,
                    "quality_reason": "",
                }
            else:
                gate = apply_deep_quality_gate(
                    discovery,
                    parsed,
                    full_text,
                    local_date=datetime.now().astimezone().date().isoformat(),
                )
            if not bool(gate["share_eligible"]):
                self._store.delete_discovery(discovery_id)
                logger.info(
                    "已移除不符合主动分享标准的候选 item=%s reason=%s",
                    discovery_id,
                    _text(gate["quality_reason"]),
                )
                return
            official_today = bool(gate["official_today"])
            heat_score = float(gate["heat_score"])
            share_score = float(gate["share_score"])
            risk_label = (_text(parsed.get("risk_label")) or _text(curated.get("risk_label"))).lower()
            screenshot_kind = _text(parsed.get("screenshot_kind")).lower()
            screenshot_reason = _text(parsed.get("screenshot_reason"))
            screenshot_base64 = ""
            screenshot_safe = _json_bool(parsed.get("screenshot_safe_for_group"))
            screenshot_worthy = _json_bool(parsed.get("screenshot_worthy"))
            # 抖音短视频会优先以 QQ 合并转发发送；不为它缓存预览图，避免随后
            # 的异步截图或多图转发在视频前后重复占用群聊版面。
            post_media = page.get("post_media") if isinstance(page.get("post_media"), dict) else {}
            if not is_douyin_video and not post_media and self.config.sharing.screenshot_enabled and screenshot_worthy:
                try:
                    post_media = await self._browser.capture_post_media(
                        url,
                        headless=self.config.browser.headless,
                    )
                except Exception as exc:
                    logger.info("帖子主图抓取失败 url=%s error=%s", url, exc)
            if post_media:
                screenshot_base64 = _text(post_media.get("image_base64"))
                screenshot_kind = _text(post_media.get("kind")) or "post_preview"
                screenshot_reason = screenshot_reason or "帖子主图能直观展示分享内容"
            elif (
                page
                and self.config.sharing.screenshot_enabled
                and not is_douyin_video
                and screenshot_safe
                and screenshot_worthy
                and screenshot_kind == "comment"
                and len(_text(parsed.get("screenshot_keyword"))) >= 8
                and random.random() <= _score(self.config.sharing.screenshot_probability, 0.9)
            ):
                try:
                    screenshot_base64 = await self._browser.capture_highlight(
                        url,
                        kind="comment",
                        keyword=_text(parsed.get("screenshot_keyword")),
                        headless=self.config.browser.headless,
                    )
                    estimated_bytes = len(screenshot_base64) * 3 // 4
                    if estimated_bytes > max(100_000, int(self.config.sharing.screenshot_max_bytes)):
                        screenshot_base64 = ""
                except Exception as exc:
                    logger.info("有趣内容截图失败 url=%s error=%s", url, exc)
            if screenshot_base64:
                estimated_bytes = len(screenshot_base64) * 3 // 4
                if estimated_bytes > max(100_000, int(self.config.sharing.screenshot_max_bytes)):
                    screenshot_base64 = ""
                    screenshot_kind = ""
            deep_result = {
                "observed_title": observed_title,
                "full_text": full_text,
                "summary": _text(parsed.get("summary")) or _text(curated.get("summary")),
                "interesting_point": _text(parsed.get("interesting_point")) or _text(curated.get("interesting_point")),
                "stance": _text(parsed.get("stance")) or _text(curated.get("stance")),
                "reasons": parsed.get("reasons") if isinstance(parsed.get("reasons"), list) else [],
                "confidence": _score(parsed.get("confidence"), _score(curated.get("confidence"), 0.6)),
                "share_score": share_score,
                "risk_label": risk_label,
                "share_intent": _text(parsed.get("share_intent")) or _text(curated.get("share_intent")),
                "official_today": official_today,
                "heat_score": heat_score,
                "share_eligible": bool(gate["share_eligible"]),
                "quality_reason": _text(gate["quality_reason"]),
                "screenshot_base64": screenshot_base64,
                "screenshot_kind": screenshot_kind if screenshot_base64 else "",
                "screenshot_reason": screenshot_reason if screenshot_base64 else "",
                "media_urls": _page_media_urls(
                    page.get("images"),
                    9,
                ),
            }
            self._store.update_deep_observation(discovery_id, deep_result)
            if not manual_douyin:
                stored = self._store.get_discoveries([discovery_id])
                if stored:
                    await self._assign_discovery_to_matching_groups(stored[0])
        except VideoDurationOutOfRangeError as exc:
            if discovery_id:
                # 时长元数据已经是确定结论；保留审计原因，但不再占用后续深读轮次。
                self._store.dismiss_discovery(discovery_id, f"视频时长不支持：{exc}")
            logger.info("跳过超出时长上限的视频 url=%s error=%s", url, exc)
        except Exception as exc:
            if discovery_id:
                self._store.mark_observation_failed(discovery_id)
            logger.warning("登录态深度阅读失败 url=%s error=%s", url, exc)

    async def _observe_video_url(self, url: str) -> dict[str, Any]:
        browser_cookies = await self._browser.cookies_for(url)
        browser_headers = await self._browser.request_headers_for(url)
        frame_samples = (
            self.config.video.douyin_frame_samples
            if "douyin.com/video" in url
            else self.config.video.frame_samples
        )
        observed = await observe_video(
            url,
            self.ctx.paths.data_dir / "video-cache",
            max_duration=self.config.candidate_filter.max_video_duration_seconds,
            frame_samples=frame_samples,
            max_subtitle_chars=self.config.video.max_subtitle_chars,
            browser_cookies=browser_cookies,
            browser_headers=browser_headers,
        )
        # 仅使用页面或下载器已提供的字幕；独立抖音冲浪插件不调用语音转写模型。
        observed.pop("audio", None)
        frame_descriptions: list[str] = []
        for frame in observed.pop("frames", []):
            result = await self._generate_vision_model(
                # Doubao Seed 2.0 Lite consumes part of the output budget on reasoning.
            # 为最终视觉描述预留足够输出空间，避免前半段分析耗尽模型输出。
                max_tokens=1200,
                temperature=0.2,
                prompt=[
                    {
                        "role": "user",
                        "content": [
                            {
                                "type": "text",
                                "text": (
                                    "只描述这个视频时间点实际可见的画面、人物动作、比分和字幕。"
                                    "不要猜测看不到的内容，不要罗列不存在的界面项目，不超过220个汉字。"
                                ),
                            },
                            {"type": "image", "image_format": "jpeg", "image_base64": base64.b64encode(frame).decode("ascii")},
                        ],
                    }
                ],
            )
            if isinstance(result, dict) and result.get("success"):
                description = llm_text(result)
                if description:
                    frame_descriptions.append(description)
        evidence_status = (
            f"字幕：{'已获取' if observed.get('subtitle') else '未获取'}；"
            f"时间轴画面：成功识别 {len(frame_descriptions)}/{frame_samples} 帧。"
        )
        prompt = (
            "你是抖音视频观察助手。请只根据实际提供的字幕、画面和元信息分析；"
            "缺少哪类证据就明确承认，不得声称自己已经读取空内容。严格输出单个 JSON 对象，"
            "不得输出思考过程、解释、Markdown 或复述整段字幕："
            '{"summary":"视频完整论述/内容脉络","visual_observations":["画面观察"],'
            '"interesting_point":"为什么值得群友注意","stance":"看完后的具体想法",'
            '"reasons":["判断理由"],"confidence":0.8,"share_score":0.8,'
            '"knowledge_score":0.8,"knowledge_facts":["视频明确支持的事实"],'
            '"share_worthy":false,"knowledge_worthy":false,'
            '"content_quality_score":0.7,"novelty_score":0.5,"utility_score":0.6,'
            '"subject_gender":"female/male/mixed/unknown","adult_subject_confirmed":false,"group_safe_suggestive":false,"mature_aesthetic_worthy":false,'
            '"official_today":false,"heat_score":0.2,"heated_comment_conflict":false,"conflict_evidence":"评论区具体冲突证据",'
            '"risk_label":"official/community/rumor/uncertain",'
            '"share_intent":"群聊中自己的切入角度","uncertainty":"缺失字幕或画面造成的限制",'
            '"screenshot_worthy":false,"screenshot_reason":"为什么当前视频画面适合作为分享预览",'
            '"screenshot_safe_for_group":false}。\n\n'
            "控制篇幅：summary、interesting_point、stance、share_intent、uncertainty、screenshot_reason 各不超过 140 个汉字；"
            "visual_observations、reasons、knowledge_facts 最多各 4 条、每条不超过 80 个汉字。\n"
            "subject_gender 只能填 female、male、mixed、unknown；没有可靠证据时填 unknown，不得猜测。"
            "内容必须适合普通群聊；出现未成年人、非自愿、偷拍、胁迫、泄露或明显成人内容时，share_worthy 必须为 false。"
            "画面或内容的分享价值应来自主题、创意、信息量或适合当前标签的趣味性。"
            "只有画面本身适合直接发到群里时 screenshot_worthy=true。"
            "年龄不明时不得擅自推断年龄或性别；缺乏可靠证据时应降低置信度。\n\n"
            f"本地日期：{datetime.now().astimezone().date().isoformat()}\n"
            f"标题：{observed.get('title', '')}\n作者：{observed.get('uploader', '')}\n"
            f"时长：{observed.get('duration', 0)} 秒\n证据状态：{evidence_status}\n简介：{observed.get('description', '')}\n"
            f"整段字幕：\n{observed.get('subtitle', '')}\n\n时间轴画面：\n{json.dumps(frame_descriptions, ensure_ascii=False)}"
        )
        async with self._background_llm_lock:
            result = await self._generate_text_model(prompt, 0.3, 2800)
        parsed = parse_json_object(llm_text(result))
        return {"video": observed, "analysis": parsed, "frame_count": len(frame_descriptions)}

    async def _list_known_streams(self) -> list[dict[str, Any]]:
        try:
            result = await self.ctx.chat.get_all_streams("qq")
        except Exception as exc:
            logger.warning("读取聊天流失败: %s", exc)
            return []
        return [item for item in result if isinstance(item, dict)] if isinstance(result, list) else []

    def _all_configured_tags(self) -> list[str]:
        """汇总所有启用群聊和私聊的标签，供一次抓取和筛选共同使用。"""

        stream_rules = self.config.sharing.stream_configs
        raw_tags = [tag for rule in stream_rules if rule.enabled for tag in rule.tags]
        tags: list[str] = []
        for raw_tag in raw_tags:
            tag = _text(raw_tag)
            if tag and tag not in tags:
                tags.append(tag)
        return tags

    def _stream_rule_for_stream(self, stream: dict[str, Any]) -> ChatSharingRule | None:
        """根据真实平台目标解析群聊或私聊规则，绝不从哈希聊天流猜测目标号码。"""

        stream_keys = self._stream_keys(stream)
        for rule in self.config.sharing.stream_configs:
            target_id = _text(rule.target_id)
            if not target_id:
                continue
            target_key = f"qq:{rule.target_type}:{target_id}"
            if target_key in stream_keys and rule.enabled:
                return rule
        return None

    async def _command_is_allowed(self, stream_id: str) -> bool:
        """只让显式授权的群聊或私聊触发会操作抖音的命令。"""

        if not self.config.command_access.enabled:
            return True
        if not stream_id:
            return False
        for stream in await self._list_known_streams():
            known_stream_id = _text(stream.get("session_id") or stream.get("stream_id"))
            if known_stream_id != stream_id:
                continue
            stream_keys = self._stream_keys(stream)
            for rule in self.config.command_access.allowed_targets:
                target_id = _text(rule.target_id)
                target_key = f"qq:{rule.target_type}:{target_id}"
                if target_id and target_key in stream_keys:
                    return True
            return False
        return False

    async def _ensure_command_allowed(self, stream_id: str):
        if await self._command_is_allowed(stream_id):
            return None
        return await self._send_command_reply(
            stream_id,
            "这个聊天还没有被加入抖音指令白名单，无法使用搜索、冲浪或浏览器登录命令。",
        )

    @staticmethod
    def _candidate_matches_tags(candidate: dict[str, Any], tags: list[str]) -> bool:
        """用审核后的主题和页面文本判断候选是否属于一个群的标签池。"""

        searchable = "\n".join(
            _text(candidate.get(key))
            for key in ("topic", "title", "summary", "snippet", "full_text", "source")
        ).casefold()
        return any(_text(tag).casefold() in searchable for tag in tags if _text(tag))

    async def _assign_discovery_to_matching_groups(self, discovery: dict[str, Any]) -> None:
        """把候选分别投放到命中标签的群聊或私聊；彼此状态不会互相影响。"""

        discovery_id = int(discovery.get("id") or 0)
        if not discovery_id:
            return
        for stream in await self._resolve_allowed_streams():
            rule = self._stream_rule_for_stream(stream)
            stream_id = _text(stream.get("session_id") or stream.get("stream_id"))
            if rule is None or not stream_id:
                continue
            if self._candidate_matches_tags(discovery, rule.tags):
                self._store.add_stream_candidate(discovery_id, stream_id)

    async def _stream_candidate_inventories(self) -> list[tuple[str, ChatSharingRule, int]]:
        """读取每个启用群聊或私聊独立的候选库存，供补货任务分别判断上下限。"""

        inventories: list[tuple[str, ChatSharingRule, int]] = []
        for stream in await self._resolve_allowed_streams():
            stream_id = _text(stream.get("session_id") or stream.get("stream_id"))
            rule = self._stream_rule_for_stream(stream)
            if stream_id and rule is not None:
                inventories.append((stream_id, rule, self._store.active_candidate_count(stream_id)))
        return inventories

    @staticmethod
    def _stream_keys(stream: dict[str, Any]) -> set[str]:
        keys: set[str] = set()
        session_id = _text(stream.get("session_id") or stream.get("stream_id"))
        platform = _text(stream.get("platform"))
        group_id = _text(stream.get("group_id"))
        user_id = _text(stream.get("user_id"))
        if session_id:
            keys.update({session_id, f"session:{session_id}"})
        if platform and group_id:
            keys.add(f"{platform}:group:{group_id}")
        if platform and user_id:
            keys.add(f"{platform}:private:{user_id}")
        return keys

    async def _resolve_allowed_streams(self) -> list[dict[str, Any]]:
        streams = await self._list_known_streams()
        # 只允许在“聊天流独立规则”中明确添加过的 QQ 群聊或私聊进入自动分享流程。
        return [stream for stream in streams if self._stream_rule_for_stream(stream) is not None]

    async def _request_douyin_authentication(self, *, reason: str, url: str = "") -> None:
        """打开可见验证窗口，并向已配置聊天流发送一次人工处理提醒。"""

        state_key = "douyin_authentication_prompt_until"
        now = time.time()
        prompt_retry_after = float(self._store.get_state(state_key, "0") or 0)
        if prompt_retry_after > now:
            return

        # 无头上下文无法操作图形验证。切换到同一浏览器档案的可见窗口后，
        # 抖音登录态和用户完成的验证都会保留在该档案中供后续自动冲浪使用。
        authentication_url = _text(url)
        login_urls = [authentication_url] if authentication_url else list(self.config.browser.login_pages)
        await self._browser.open_login_windows(login_urls)
        self._store.set_state(state_key, now + _DOUYIN_AUTHENTICATION_COOLDOWN_SECONDS)

        notice = (
            "⚠️ 抖音冲浪遇到登录或图形安全验证，已自动打开插件专用浏览器。"
            "请在弹出的抖音页面完成验证；完成后插件会自动继续冲浪。"
        )
        for stream in await self._resolve_allowed_streams():
            stream_id = _text(stream.get("session_id") or stream.get("stream_id"))
            if stream_id:
                await self.ctx.send.text(notice, stream_id)
        logger.warning("已请求人工完成抖音验证 reason=%s", reason)

    async def _qq_group_id_for_stream(self, stream_id: str) -> str:
        """从已注册的真实聊天流解析 QQ 群号，绝不从哈希流 ID 猜测。"""
        for stream in await self._list_known_streams():
            known_stream_id = _text(stream.get("session_id") or stream.get("stream_id"))
            platform = _text(stream.get("platform")).lower()
            group_id = _text(stream.get("group_id"))
            if known_stream_id == stream_id and platform == "qq" and group_id.isdigit():
                return group_id
        return ""

    async def _stream_is_shareable(self, stream_id: str, rule: ChatSharingRule | None = None) -> bool:
        # 自动分享只面向明确配置过的目标；不存在“全局默认规则”兜底。
        if rule is None or not rule.enabled:
            return False
        active_hours = rule.active_hours
        daily_limit = rule.daily_limit
        cooldown_hours = rule.cooldown_hours
        min_quiet_minutes = rule.min_quiet_minutes
        now = datetime.now()
        if not _within_active_hours(now, active_hours):
            return False
        day_start = datetime.combine(now.date(), datetime.min.time()).timestamp()
        daily_limit = max(0, int(daily_limit))
        if daily_limit and len(self._store.recent_shared(stream_id, day_start)) >= daily_limit:
            return False

        # 自动分享采用“每个聊天流一个冷却窗口”：避免多种候选类型轮流绕过频率限制。
        cooldown_since = time.time() - max(0.25, float(cooldown_hours)) * 3600
        if self._store.recent_shared(stream_id, cooldown_since):
            return False

        # 只检查“最近有没有聊天”，不因群长期安静而停止推送。
        history_start = time.time() - 7 * 24 * 3600
        try:
            messages = await self.ctx.message.get_by_time_in_chat(
                stream_id,
                str(history_start),
                str(time.time()),
                limit=20,
                limit_mode="latest",
                filter_mai=False,
                filter_command=False,
            )
        except Exception as exc:
            logger.debug("读取群聊活跃度失败 stream=%s error=%s", stream_id, exc)
            return False
        if not isinstance(messages, list) or not messages:
            return True
        latest = max((_message_timestamp(item) for item in messages), default=0.0)
        if latest <= 0:
            return True
        silence_seconds = time.time() - latest
        return silence_seconds >= max(1, int(min_quiet_minutes)) * 60

    async def _maybe_trigger_share(self) -> bool:
        streams = await self._resolve_allowed_streams()
        for stream in streams:
            stream_id = _text(stream.get("session_id") or stream.get("stream_id"))
            if not stream_id or stream_id in self._pending_share:
                continue
            stream_rule = self._stream_rule_for_stream(stream)
            if not await self._stream_is_shareable(stream_id, stream_rule):
                continue
            day_start = datetime.combine(datetime.now().date(), datetime.min.time()).timestamp()
            shared_today = self._store.recent_shared(stream_id, day_start)
            normal_cooldown_since = time.time() - max(
                0.25,
                float(stream_rule.cooldown_hours),
            ) * 3600
            normal_share_recent = bool(self._store.recent_shared(stream_id, normal_cooldown_since))
            candidate = None if normal_share_recent else self._store.next_share_candidate(
                self.config.sharing.minimum_share_score,
                stream_id=stream_id,
                selection_mode=self.config.sharing.candidate_selection_mode,
            )
            if candidate is None:
                logger.info("本轮没有达到质量线的抖音候选 stream=%s", stream_id)
                continue
            discovery_id = int(candidate["id"])
            if _is_douyin_note(candidate) and not self.config.candidate_filter.allow_douyin_notes:
                self._store.dismiss_discovery(discovery_id, "已关闭抖音图文候选")
                logger.info("已跳过历史图文候选 item=%s stream=%s", discovery_id, stream_id)
                continue
            intent = (
                "这是已通过内容质量、分享时段、冷却和额度检查的抖音主动分享任务，必须调用 reply 完成分享。"
                "原帖和链接会放在回复前；只补一到三句自然、简短的分享感想，不要编造事实或复述原帖。"
            )
            metadata = {
                _ITEM_ARG: discovery_id,
                "source": candidate.get("source", ""),
                "title": candidate.get("title", ""),
                "url": candidate.get("url", ""),
                "summary": candidate.get("summary", ""),
                "interesting_point": candidate.get("interesting_point", ""),
                "risk_label": candidate.get("risk_label", ""),
                "share_intent": candidate.get("share_intent", ""),
            }
            self._store.mark_share_queued(discovery_id, stream_id)
            self._pending_share[stream_id] = (discovery_id, time.time())
            try:
                await self.ctx.maisaka.proactive.trigger(
                    stream_id,
                    intent,
                    reason=f"发现高价值社区见闻：{candidate.get('title', '')}",
                    priority="normal",
                    metadata=metadata,
                )
            except Exception:
                self._pending_share.pop(stream_id, None)
                self._store.restore_share_candidate(discovery_id, stream_id)
                raise
            logger.info("已唤醒抖音候选分享 item=%s stream=%s", discovery_id, stream_id)
            return True
        return False

    def _defer_declined_share(self, discovery_id: int, stream_id: str, reason: str) -> None:
        """在 Planner 未选择分享时记录候选退避，避免每分钟重复唤醒。"""

        outcome = self._store.defer_share_candidate(
            discovery_id,
            stream_id,
            cooldown_minutes=self.config.sharing.declined_share_cooldown_minutes,
            max_attempts=self.config.sharing.declined_share_max_attempts,
            reason=reason,
        )
        logger.info(
            "主动分享候选未发送，已%s item=%s stream=%s reason=%s",
            "停止" if outcome == "dismissed" else "暂缓",
            discovery_id,
            stream_id,
            reason,
        )

    @HookHandler(
        "maisaka.planner.before_request",
        name="inject_douyin_share_context",
        description="向 Planner 注入抖音候选自动分享上下文",
        mode=HookMode.BLOCKING,
        order=HookOrder.LATE,
        error_policy=ErrorPolicy.SKIP,
    )
    async def inject_self_continuity(self, **kwargs: Any) -> dict[str, Any]:
        if not self._enabled():
            return {"action": "continue"}
        messages = kwargs.get("items")
        session_id = _text(kwargs.get("session_id"))
        if not isinstance(messages, list) or not session_id:
            return {"action": "continue"}
        is_proactive_share = _is_douyin_surf_proactive_request(messages)
        if is_proactive_share:
            self._active_proactive_share_sessions.add(session_id)
        else:
            self._active_proactive_share_sessions.discard(session_id)
            # A pending share belongs only to its synthetic proactive turn. If a
            # genuine chat turn has already arrived, never let that old item bind
            # to the human's new message.
            stale_pending = self._pending_share.pop(session_id, None)
            if stale_pending is not None:
                self._defer_declined_share(stale_pending[0], session_id, "被新的群消息打断")
                logger.info(
                    "已回收被新群消息打断的待分享见闻 item=%s stream=%s",
                    stale_pending[0],
                    session_id,
                )
            # 本插件不参与普通聊天的 Planner。只有由抖音候选触发的主动
            # 只有主动分享任务需要注入上下文，避免影响 MaiBot 的普通聊天人格。
            return {"action": "continue"}
        context = (
            "【抖音候选自动分享】\n"
            f"筛选规则：{self.config.identity.values}\n"
            f"表达方式：{self.config.identity.reply_style}\n"
            "这是独立的候选分享任务，不参与日程、情绪、关系、动态或普通聊天。"
        )
        self._planner_share_context[session_id] = context
        updated_messages = list(messages)
        updated_messages.append(
            {
                "item_type": "UserMessageItem",
                "meta": {
                    "item_id": uuid.uuid4().hex,
                    "logical_turn_id": None,
                    "timestamp": datetime.now().isoformat(),
                },
                "parts": [{"type": "text", "text": context}],
            }
        )
        result = dict(kwargs)
        result["items"] = updated_messages
        return {"action": "continue", "modified_kwargs": result}

    @HookHandler(
        "maisaka.planner.after_response",
        name="attach_douyin_share_context_to_reply",
        description="把待分享的抖音候选上下文透传给 Replyer",
        mode=HookMode.BLOCKING,
        order=HookOrder.LATE,
        error_policy=ErrorPolicy.SKIP,
    )
    async def attach_share_context_to_reply(self, **kwargs: Any) -> dict[str, Any]:
        if not self._enabled():
            return {"action": "continue"}
        tool_calls = kwargs.get("output_items")
        session_id = _text(kwargs.get("session_id"))
        if not isinstance(tool_calls, list) or not session_id:
            return {"action": "continue"}

        if session_id not in self._active_proactive_share_sessions:
            return {"action": "continue"}

        pending = (
            self._pending_share.get(session_id)
            if session_id in self._active_proactive_share_sessions
            else None
        )
        if pending is not None and time.time() - pending[1] > _PENDING_MAX_AGE_SECONDS:
            self._defer_declined_share(pending[0], session_id, "Planner 决策超时")
            self._pending_share.pop(session_id, None)
            pending = None
        share_context = self._planner_share_context.get(session_id, "")
        suppress_sticker_reaction = False
        modified_calls: list[Any] = []
        reply_found = False
        any_tool_call = False
        for raw_call in tool_calls:
            if not isinstance(raw_call, dict):
                modified_calls.append(raw_call)
                continue
            call = dict(raw_call)
            function = call.get("tool_call")
            function_name = _text(function.get("func_name")) if isinstance(function, dict) else ""
            any_tool_call = any_tool_call or bool(function_name)
            if suppress_sticker_reaction and function_name in {"reply", "send_emoji"}:
                logger.info("忽略无点名纯表情触发的 %s，避免强行续聊", function_name)
                continue
            if not isinstance(function, dict) or function_name != "reply":
                modified_calls.append(call)
                continue
            reply_found = True
            function = dict(function)
            arguments = function.get("args")
            arguments = dict(arguments) if isinstance(arguments, dict) else {}
            reference_parts = [
                _text(arguments.get("reply_reference")),
                share_context,
                _reply_style_context(self.config.identity.reply_style),
            ]
            if pending is not None:
                discovery = self._store.get_discoveries([pending[0]])
                item = discovery[0] if discovery else {}
                item_reference = (
                    "【本轮已完整阅读的自主见闻】\n"
                    f"来源：{item.get('source', '')}\n标题：{item.get('title', '')}\n链接：{item.get('url', '')}\n"
                    f"完整阅读后的摘要：{item.get('summary', '')}\n信息性质：{item.get('risk_label', '')}\n"
                    f"当天官方新内容：{bool(item.get('official_today'))}\n争议热度：{item.get('heat_score', 0)}\n"
                    f"值得说的角度：{item.get('interesting_point', '')}\n分享意图：{item.get('share_intent', '')}\n"
                    "原帖会由插件自动放在最终消息前面。你的回复只写自己的自然反应，不要复述或解说正文。"
                )
                reference_parts.append(item_reference)
                arguments[_ITEM_ARG] = pending[0]
                if not _text(arguments.get("new_contribution")):
                    arguments["new_contribution"] = _text(item.get("stance"))
            arguments["reply_reference"] = "\n\n".join(part for part in reference_parts if part)
            function["args"] = arguments
            call["tool_call"] = function
            modified_calls.append(call)

        if pending is not None and not reply_found and not any_tool_call:
            self._defer_declined_share(pending[0], session_id, "Planner 判断当前不分享")
            self._pending_share.pop(session_id, None)
        if reply_found or not any_tool_call:
            self._active_proactive_share_sessions.discard(session_id)
        if modified_calls == tool_calls:
            return {"action": "continue"}
        result = dict(kwargs)
        result["output_items"] = modified_calls
        return {"action": "continue", "modified_kwargs": result}

    @HookHandler(
        "maisaka.replyer.before_model_request",
        name="isolate_douyin_share_replyer_context",
        description="自主分享不继承普通互动的目标消息与聊天上下文",
        mode=HookMode.BLOCKING,
        order=HookOrder.LATE,
        error_policy=ErrorPolicy.SKIP,
    )
    async def isolate_proactive_share_replyer_context(self, **kwargs: Any) -> dict[str, Any]:
        """为主动分享构造独立的 Replyer 上下文，避免误接最后一条群消息。"""

        if not self._enabled():
            return {"action": "continue"}
        reply_tool_args = kwargs.get("reply_tool_args")
        if not isinstance(reply_tool_args, dict):
            return {"action": "continue"}
        try:
            item_id = int(reply_tool_args.get(_ITEM_ARG) or 0)
        except (TypeError, ValueError):
            item_id = 0
        if item_id <= 0:
            return {"action": "continue"}

        rows = self._store.get_discoveries([item_id])
        item = rows[0] if rows else {}
        if not item:
            return {"action": "continue"}
        items = kwargs.get("items")
        if not isinstance(items, list):
            return {"action": "continue"}

        # Reply 工具必须带一个真实 msg_id 才能发送，但主动分享不是对这条消息的回答。
        # 只保留 Replyer 的系统人格，并替换为独立的分享任务，避免历史中的互动话题污染评论。
        system_items = [
            raw_item
            for raw_item in items
            if isinstance(raw_item, dict) and raw_item.get("item_type") == "SystemMessageItem"
        ]
        if not system_items:
            logger.warning("主动分享缺少 Replyer 系统提示，保留原上下文 item=%s", item_id)
            return {"action": "continue"}
        share_context = (
            "【独立主动分享任务】\n"
            "这不是对任何群友消息的回复。不要回答、承接、评价或提及此前群聊中的任何人、"
            "角色、游戏、问题或表情；它们与本次分享无关。\n"
            "插件会自动先发送原帖截图（如有）并在你的文字前附上原帖主体与链接。"
            "你只写一到三句像熟人群友顺手转帖后的自然感想，不要复述原帖、不要写“原帖：”或链接，"
            "更不要假装在回复某人的上一句话。\n"
            f"来源：{_text(item.get('source'))}\n"
            f"标题：{_text(item.get('title'))}\n"
            f"完整阅读摘要：{_text(item.get('summary'))}\n"
            f"值得说的角度：{_text(item.get('interesting_point'))}\n"
            f"自己的判断：{_text(item.get('stance'))}\n"
            f"分享意图：{_text(item.get('share_intent'))}"
        )
        isolated_items = [
            system_items[0],
            {
                "item_type": "UserMessageItem",
                "meta": {
                    "item_id": uuid.uuid4().hex,
                    "logical_turn_id": None,
                    "timestamp": datetime.now().isoformat(),
                },
                "parts": [{"type": "text", "text": share_context}],
            },
        ]
        result = dict(kwargs)
        result["items"] = isolated_items
        return {"action": "continue", "modified_kwargs": result}

    @HookHandler(
        "maisaka.reply.before_post_process",
        name="prepend_douyin_forwarded_post",
        description="在分享短评前附上原帖主体和链接",
        mode=HookMode.BLOCKING,
        order=HookOrder.LATE,
        error_policy=ErrorPolicy.SKIP,
    )
    async def prepend_forwarded_post(self, **kwargs: Any) -> dict[str, Any]:
        if not self._enabled():
            return {"action": "continue"}
        session_id = _text(kwargs.get("session_id"))
        arguments = kwargs.get("reply_tool_args")
        arguments = dict(arguments) if isinstance(arguments, dict) else {}
        try:
            item_id = int(arguments.get(_ITEM_ARG) or 0)
        except (TypeError, ValueError):
            item_id = 0
        if not item_id:
            return {"action": "continue"}
        rows = self._store.get_discoveries([item_id])
        item = rows[0] if rows else {}
        body = _text(item.get("full_text"))
        url = _text(item.get("url"))
        response = _text(kwargs.get("response"))
        if not response or response.startswith("【转发自"):
            return {"action": "continue"}
        if not body or not url:
            # 主动分享的附言若没有对应来源会造成误解，因此绝不单独发送。
            # be allowed to escape as a normal chat message.
            self._defer_declined_share(item_id, session_id, "转发正文不完整")
            self._pending_share.pop(session_id, None)
            result = dict(kwargs)
            result["response"] = ""
            logger.error(
                "已阻止正文不完整的主动分享: item=%s stream=%s body=%s url=%s",
                item_id,
                session_id,
                bool(body),
                bool(url),
            )
            return {"action": "continue", "modified_kwargs": result}
        if not _share_comment_is_relevant(response, item) or _looks_like_internal_share_analysis(response):
            logger.warning(
                "已替换无关或内部分析风格的主动分享说明: "
                "item=%s stream=%s response=%r",
                item_id,
                session_id,
                response[:160],
            )
            response = _fallback_share_comment(item)
        result = dict(kwargs)
        result["response"] = _format_share_message(
            item,
            response,
            self.config.sharing.forward_body_max_chars,
        )
        is_douyin_candidate = _is_douyin_candidate(item)
        video_forwarded = await self._forward_douyin_share_video(item_id, session_id, item)
        note_images_forwarded = (
            await self._forward_douyin_note_images(item_id, session_id, item)
            if is_douyin_candidate and not video_forwarded
            else False
        )
        # 抖音短视频只能发送原生视频、抖音图文只能发送原帖图片；不能再走截图
        # 或其他站点的合并转发逻辑，避免同一作品重复出现静态预览。
        # 通用版不依赖额外媒体转发接口，分享内容直接由插件发送。
        media_forwarded = False
        if is_douyin_candidate and not video_forwarded and not note_images_forwarded:
            # 通用版不依赖作者本地的媒体转发插件。没有可用媒体能力时仍发送
            # 原链接和简短说明，确保候选不会因可选增强缺失而被错误丢弃。
            result["response"] = f"{_text(result['response'])}\n原链接：{_text(item.get('url'))}"
            logger.info(
                "抖音媒体未转发，改为发送原链接 item=%s stream=%s",
                item_id,
                session_id,
            )
        screenshot_base64 = "" if is_douyin_candidate else _text(item.get("screenshot_base64"))
        screenshot_key = (item_id, session_id)
        if (
            screenshot_base64
            and not video_forwarded
            and not media_forwarded
            and session_id
            and not item.get("screenshot_shared_at")
            and screenshot_key not in self._pending_screenshot_sends
        ):
            # 先发帖子截图、随后由正常 Reply 流程发一句评论，视觉顺序更像群友转图。
            self._pending_screenshot_sends.add(screenshot_key)
            await self._send_share_screenshot(item_id, session_id, screenshot_base64, delay_seconds=0)
        result["skip_post_process"] = True
        result["enable_splitter"] = False
        result["enable_chinese_typo"] = False
        return {"action": "continue", "modified_kwargs": result}

    async def _forward_douyin_share_video(
        self,
        discovery_id: int,
        session_id: str,
        item: dict[str, Any],
    ) -> bool:
        """下载合格抖音短视频，并通过所选 QQ 适配器发送原生视频。

        这里不依赖额外的媒体转发插件。未安装所选适配器、不是 QQ 群，或平台
        拒绝视频时返回 ``False``，调用方会发送原链接。
        """

        url = _text(item.get("url"))
        if (
            not self.config.sharing.douyin_video_forward_enabled
            or not session_id
            or item.get("video_shared_at")
            or "douyin.com/video/" not in url
        ):
            return False
        try:
            browser_cookies = await self._browser.cookies_for(url)
            browser_headers = await self._browser.request_headers_for(url)
            downloaded = await download_short_video_for_share(
                url,
                self.ctx.paths.data_dir / "video-share-cache",
                max_duration=self.config.candidate_filter.max_video_duration_seconds,
                max_bytes=self.config.sharing.douyin_video_max_bytes,
                browser_cookies=browser_cookies,
                browser_headers=browser_headers,
            )
            group_id = await self._qq_group_id_for_stream(session_id)
            if not group_id:
                logger.info(
                    "当前分享目标不是可解析的 QQ 群，跳过抖音原生视频发送 discovery_id=%s stream=%s",
                    discovery_id,
                    session_id,
                )
                return False
            title = _text(item.get("observed_title")) or _text(item.get("title"))
            segments = [
                {
                    "type": "text",
                    "data": {"text": f"{title}\n原链接：{url}".strip()},
                },
                {
                    "type": "video",
                    "data": {"file": f"base64://{downloaded['video_base64']}"},
                },
            ]
            payload = {"group_id": int(group_id), "message": segments}
            sent_by = ""
            failure_reasons: list[str] = []
            for api_name in _video_sender_api_names(self.config.sharing.video_sender_adapter):
                try:
                    api_result = await asyncio.wait_for(
                        self.ctx.api.call(api_name, version="1", params=payload),
                        timeout=45,
                    )
                except Exception as exc:
                    failure_reasons.append(f"{api_name}: {exc}")
                    continue
                if isinstance(api_result, dict) and api_result.get("status") != "failed":
                    sent_by = api_name
                    break
                reason = "适配器未返回有效结果"
                if isinstance(api_result, dict):
                    reason = _text(api_result.get("message") or api_result.get("wording")) or reason
                failure_reasons.append(f"{api_name}: {reason}")
            if not sent_by:
                logger.error(
                    "抖音原生视频发送被适配器拒绝 discovery_id=%s stream=%s reason=%s",
                    discovery_id,
                    session_id,
                    "；".join(failure_reasons) or "未调用到可用适配器",
                )
                return False
        except VideoDurationOutOfRangeError as exc:
            logger.info("抖音短视频超出分享时长，取消本次分享 discovery_id=%s error=%s", discovery_id, exc)
            return False
        except VideoFileTooLargeError as exc:
            logger.info("抖音短视频体积过大，取消本次分享 discovery_id=%s error=%s", discovery_id, exc)
            return False
        except Exception:
            logger.exception("抖音短视频下载或发送失败 discovery_id=%s stream=%s", discovery_id, session_id)
            return False
        self._store.mark_video_shared(discovery_id)
        logger.info(
            "抖音短视频已通过 %s 作为 QQ 原生视频发送 discovery_id=%s duration=%ss",
            sent_by,
            discovery_id,
            downloaded["duration"],
        )
        return True

    async def _forward_douyin_note_images(
        self,
        discovery_id: int,
        session_id: str,
        item: dict[str, Any],
    ) -> bool:
        """下载已打开图文笔记中的原图，并通过一条图文混合消息发送。"""

        if (
            not self.config.candidate_filter.allow_douyin_notes
            or not session_id
            or not _is_douyin_note(item)
            or item.get("media_forwarded_at")
        ):
            return False
        url = _text(item.get("url"))
        try:
            try:
                stored_urls = json.loads(_text(item.get("media_urls_json")) or "[]")
            except json.JSONDecodeError:
                stored_urls = []
            image_urls = [str(image_url) for image_url in stored_urls if isinstance(image_url, str)]
            if not image_urls:
                page = await self._browser.read_page(
                    url,
                    headless=self.config.browser.headless,
                    max_chars=self.config.browser.max_text_chars,
                )
                image_urls = _page_media_urls(page.get("images"), 9)
            if not image_urls:
                logger.info("抖音图文未读取到可下载的正文图片 item=%s", discovery_id)
                return False
            browser_cookies = await self._browser.cookies_for(url)
            images_base64 = await download_images_for_share(
                image_urls,
                max_images=9,
                max_bytes_per_image=self.config.sharing.screenshot_max_bytes,
                browser_cookies=browser_cookies,
            )
            if not images_base64:
                logger.info("抖音图文正文图片全部下载失败 item=%s", discovery_id)
                return False
            title = _text(item.get("observed_title")) or _text(item.get("title"))
            segments: list[dict[str, Any]] = [
                {"type": "text", "content": f"{title}\n原链接：{url}".strip()},
                *({"type": "image", "content": image_base64} for image_base64 in images_base64),
            ]
            sent = await self.ctx.send.hybrid(segments, session_id)
            sent_successfully = bool(sent.get("sent")) if isinstance(sent, dict) else bool(sent)
            if not sent_successfully:
                logger.warning("抖音图文混合消息发送失败 item=%s stream=%s", discovery_id, session_id)
                return False
        except Exception:
            logger.exception("抖音图文下载或混合消息发送失败 item=%s stream=%s", discovery_id, session_id)
            return False
        self._store.mark_media_forwarded(discovery_id)
        logger.info(
            "抖音图文已作为图文混合消息发送 item=%s stream=%s images=%s",
            discovery_id,
            session_id,
            len(images_base64),
        )
        return True

    async def _forward_share_media(self, discovery_id: int, session_id: str, item: dict[str, Any]) -> bool:
        """通用版不调用第三方媒体接口，保留插件自身的截图降级路径。"""

        del discovery_id, session_id, item
        return False

    async def _send_share_screenshot(
        self,
        discovery_id: int,
        session_id: str,
        image_base64: str,
        *,
        delay_seconds: float = 1.5,
    ) -> None:
        key = (int(discovery_id), _text(session_id))
        try:
            if delay_seconds > 0:
                await asyncio.sleep(delay_seconds)
            # 已经入队的旧截图任务也必须在实际发送前重新核对来源。否则视频
            # 发送与截图延迟任务并发时，截图可能在 video_shared_at 落库前发出。
            discoveries = self._store.get_discoveries([discovery_id])
            if discoveries and _is_douyin_candidate(discoveries[0]):
                logger.info("抖音视频禁止发送静态预览 discovery_id=%s", discovery_id)
                return
            await self.ctx.send.image(image_base64, session_id)
            self._store.mark_screenshot_shared(discovery_id)
        except Exception:
            logger.exception("分享网页局部截图失败 discovery_id=%s stream=%s", discovery_id, session_id)
        finally:
            self._pending_screenshot_sends.discard(key)

    @HookHandler(
        "maisaka.reply.before_post_process",
        name="confirm_douyin_share_after_reply",
        description="在实际回复形成后确认抖音候选已分享",
        mode=HookMode.OBSERVE,
        order=HookOrder.LATE,
        error_policy=ErrorPolicy.SKIP,
    )
    async def learn_after_reply(self, **kwargs: Any) -> dict[str, Any]:
        if not self._enabled():
            return {"action": "continue"}
        response = _strip_forwarded_source(_text(kwargs.get("response")))
        session_id = _text(kwargs.get("session_id"))
        arguments = kwargs.get("reply_tool_args")
        arguments = dict(arguments) if isinstance(arguments, dict) else {}
        try:
            item_id = int(arguments.get(_ITEM_ARG) or 0)
        except (TypeError, ValueError):
            item_id = 0
        if not item_id:
            return {"action": "continue"}
        if session_id and response:
            self._store.mark_shared(item_id, session_id)
            discovery = self._store.get_discoveries([item_id])
            item = discovery[0] if discovery else {}
            screenshot_base64 = _text(item.get("screenshot_base64"))
            screenshot_key = (item_id, session_id)
            if (
                screenshot_base64
                and not _is_douyin_candidate(item)
                and not item.get("video_shared_at")
                and not item.get("screenshot_shared_at")
                and screenshot_key not in self._pending_screenshot_sends
            ):
                self._pending_screenshot_sends.add(screenshot_key)
                self._track_task(self._send_share_screenshot(item_id, session_id, screenshot_base64))
            self._pending_share.pop(session_id, None)
        return {"action": "continue"}

    @EventHandler(
        "observe_reactions_to_douyin_shares",
        description="记录群友对抖音主动分享内容的自然反馈",
        event_type=EventType.ON_MESSAGE,
    )
    async def observe_share_reactions(self, message: Any = None, stream_id: str = "", **kwargs: Any):
        del kwargs
        if not self._enabled() or not stream_id:
            return True, True, None, None, None
        text = _message_plain_text(message)
        if not text or text.startswith("/"):
            return True, True, None, None, None
        since = time.time() - max(5, int(self.config.sharing.reaction_window_minutes)) * 60
        shared = self._store.recent_shared(stream_id, since)
        if shared:
            self._store.add_reaction(int(shared[0]["id"]), stream_id, text)
        return True, True, None, None, None

    @Command("douyin_surf_manual", description="立即进行一轮抖音推荐流冲浪", pattern=r"^/(?:抖音冲浪|douyin_surf)$")
    async def command_surf(self, stream_id: str = "", **kwargs: Any):
        del kwargs
        if not self._enabled():
            return await self._send_command_reply(stream_id, "抖音冲浪与分享插件未启用")
        denied = await self._ensure_command_allowed(stream_id)
        if denied is not None:
            return denied
        self._track_task(self._manual_surf_and_notify(stream_id))
        return await self._send_command_reply(stream_id, "开始翻抖音推荐流了，筛完合格内容再回来。")

    async def _manual_surf_and_notify(self, stream_id: str) -> None:
        try:
            result = await self._run_surf_cycle(manual=True)
            if stream_id:
                await self.ctx.send.text(_format_surf_result_message(result), stream_id)
            await self._maybe_trigger_share()
        except Exception as exc:
            logger.exception("手动冲浪失败")
            if stream_id:
                await self.ctx.send.text(f"这次出去转了一圈没顺利回来：{exc}", stream_id)

    @Command(
        "douyin_surf_search",
        description="按标签搜索抖音，从合格候选中挑一条发到当前聊天",
        pattern=r"^/抖音(?:\s+(?P<query>.+?))?\s*$",
    )
    async def command_douyin_search(
        self,
        stream_id: str = "",
        matched_groups: dict[str, str] | None = None,
        **kwargs: Any,
    ):
        del kwargs
        query = _text((matched_groups or {}).get("query"))
        if not query:
            return await self._send_command_reply(stream_id, "用法：/抖音 正常穿搭。插件会从合格搜索结果里挑一条。")
        if len(query) > 60:
            return await self._send_command_reply(stream_id, "标签太长啦，控制在 60 个字以内再搜。")
        if not self._enabled() or not self.config.browser.enabled:
            return await self._send_command_reply(stream_id, "抖音浏览器尚未启动。")
        denied = await self._ensure_command_allowed(stream_id)
        if denied is not None:
            return denied
        self._track_task(self._manual_douyin_search_and_share(query, stream_id))
        return await self._send_command_reply(stream_id, f"我去翻翻「{query}」，从综合页里挑点赞最高的。")

    async def _manual_douyin_search_and_share(self, query: str, stream_id: str) -> None:
        """从综合页候选中按公开点赞排序，直接分享可发送的最高项。"""

        try:
            search_deadline = (
                asyncio.get_running_loop().time()
                + self.config.browser.manual_douyin_search_timeout_seconds
            )
            target_results = self.config.browser.manual_douyin_target_results
            result_limit = max(self.config.browser.manual_douyin_search_results, target_results)
            try:
                results = await self._browser.discover_douyin_search(
                    keyword=query,
                    # 手动 /抖音 只浏览综合页；首批均已分享时，下面会排除它们
                    # 并继续向下翻阅同一搜索结果，而不是混入视频页结果。
                    search_type="general",
                    max_results=result_limit,
                    scroll_rounds=0,
                    # 手动点播与自动抖音冲浪共用同一个隐藏窗口开关，避免设置互相矛盾。
                    headless=self.config.browser.headless,
                    # 首屏达到目标就不必额外下拉；不足时持续累积合格候选，直到
                    # 达标、触底或耗尽整次命令的搜索时间预算。
                    target_result_count=target_results,
                    search_timeout_seconds=self.config.browser.manual_douyin_search_timeout_seconds,
                    search_deadline_monotonic=search_deadline,
                    # 页面最先出现的少量链接可能只是导航或推荐卡。手动点播多等待数秒
                    # 让本次关键词的自然响应到齐，避免把“还在加载”误判成“没有结果”。
                    initial_result_wait_ms=4_000,
                    min_like_count=self.config.candidate_filter.min_like_count,
                    allow_douyin_notes=self.config.candidate_filter.allow_douyin_notes,
                    max_video_duration_seconds=self.config.candidate_filter.max_video_duration_seconds,
                    allow_low_metadata_results=True,
                )
            except DouyinSearchNoResultError as exc:
                # 手动点播只使用综合页：视频页的混合结果与当前标签相关性较弱，
                # 不把它作为保底来源，避免为了凑数引入不相符的作品。
                logger.info("手动抖音综合页没有候选，不切换视频页 query=%s reason=%s", query, exc)
                results = []
            for item in results:
                item["source"] = "手动·抖音"
            self._store.add_candidates(results)
            searched_result_count = len(results)
            stored_results = self._store.get_discoveries_by_urls(
                _text(result.get("url")) for result in results
            )
            candidate_ids = [
                int(item["id"])
                for item in stored_results
                # 手动点播可复用此前浏览过、但没有成功发送的候选；只排除真正
                # 发过媒体的作品，避免同一个视频反复占用群聊版面。
                if not item.get("shared_at") and not item.get("video_shared_at")
            ]
            if not candidate_ids and results:
                # 首批都已分享时，必须把它们交给浏览器层排除。否则每次重新打开
                # 搜索页都会再次从同一批响应里取前 N 条，所谓“补充下拉”实际没有
                # 机会抵达后面的新作品。
                excluded_urls = {
                    _text(item.get("url"))
                    for item in stored_results
                    if item.get("shared_at") or item.get("video_shared_at")
                }
                logger.info(
                    "手动抖音首批无未分享候选，排除已发作品后继续下拉 query=%s excluded=%s",
                    query,
                    len(excluded_urls),
                )
                remaining_seconds = max(0, int(search_deadline - asyncio.get_running_loop().time()))
                if not remaining_seconds:
                    extra_results = []
                else:
                    try:
                        extra_results = await self._browser.discover_douyin_search(
                            keyword=query,
                            search_type="general",
                            # 排除已发内容后继续在综合页累计新的合格候选；仍与首轮
                            # 共用搜索截止时间，不能因为去重而额外等待五分钟。
                            max_results=min(20, result_limit),
                            scroll_rounds=0,
                            headless=self.config.browser.headless,
                            target_result_count=target_results,
                            search_timeout_seconds=remaining_seconds,
                            search_deadline_monotonic=search_deadline,
                            initial_result_wait_ms=4_000,
                            min_like_count=self.config.candidate_filter.min_like_count,
                            allow_douyin_notes=self.config.candidate_filter.allow_douyin_notes,
                            max_video_duration_seconds=self.config.candidate_filter.max_video_duration_seconds,
                            allow_low_metadata_results=True,
                            excluded_urls=excluded_urls,
                        )
                    except DouyinSearchNoResultError as exc:
                        logger.info("手动抖音综合页补充下拉没有候选 query=%s reason=%s", query, exc)
                        extra_results = []
                for item in extra_results:
                    item["source"] = "手动·抖音"
                self._store.add_candidates(extra_results)
                searched_result_count += len(extra_results)
                candidate_ids = [
                    int(item["id"])
                    for item in self._store.get_discoveries_by_urls(
                        _text(result.get("url")) for result in extra_results
                    )
                    if not item.get("shared_at") and not item.get("video_shared_at")
                ]
            if not candidate_ids:
                if searched_result_count:
                    await self.ctx.send.text(
                        f"「{query}」这批综合页可解析的作品都已经发过了，这次就不重复刷屏。",
                        stream_id,
                    )
                else:
                    await self.ctx.send.text(
                        f"「{query}」综合页这次没有解析到自然作品；不是发过了。"
                        "我已经继续向下翻过，仍没有拿到能分享的新视频。",
                        stream_id,
                    )
                return
            # 即时点播只按搜索响应的公开点赞数排序；不走 background、VLM 或
            # 逐条详情深读，避免一次命令因候选失败而等待数分钟。
            def like_count(item: dict[str, Any]) -> int:
                match = re.search(r"点赞：\s*(\d+)", _text(item.get("snippet")))
                return int(match.group(1)) if match else -1

            ranked = sorted(self._store.get_discoveries(candidate_ids), key=like_count, reverse=True)
            attempted_count = 0
            for candidate in ranked:
                discovery_id = int(candidate["id"])
                if _is_douyin_note(candidate) and not self.config.candidate_filter.allow_douyin_notes:
                    self._store.dismiss_discovery(discovery_id, "手动抖音仅发送视频，已跳过图文笔记")
                    continue
                candidate_url = _text(candidate.get("url"))
                if "douyin.com/video/" in candidate_url:
                    browser_cookies = await self._browser.cookies_for(candidate_url)
                    browser_headers = await self._browser.request_headers_for(candidate_url)
                    duration = await probe_video_duration(
                        candidate_url,
                        self.ctx.paths.data_dir / "video-duration-probe-cache",
                        browser_cookies=browser_cookies,
                        browser_headers=browser_headers,
                    )
                    max_duration = self.config.candidate_filter.max_video_duration_seconds
                    if duration <= 0 or duration > max_duration:
                        self._store.dismiss_discovery(
                            discovery_id,
                            f"手动抖音视频时长 {duration or '未知'} 秒，不符合候选上限 {max_duration} 秒",
                        )
                        logger.info(
                            "手动抖音最终发送前跳过超长或时长未知视频 item=%s duration=%s max_duration=%s",
                            discovery_id,
                            duration,
                            max_duration,
                        )
                        continue
                if _json_bool(candidate.get("unsafe")):
                    self._store.dismiss_discovery(discovery_id, "手动抖音候选存在明显安全风险")
                    continue
                attempted_count += 1
                video_forwarded = await self._forward_douyin_share_video(discovery_id, stream_id, candidate)
                note_images_forwarded = (
                    await self._forward_douyin_note_images(discovery_id, stream_id, candidate)
                    if not video_forwarded
                    else False
                )
                if not video_forwarded and not note_images_forwarded:
                    # 媒体转发是可选增强。没有兼容 API 或下载失败时仍返回原链接，
                    # 让非 QQ 平台和纯文本安装也能正常使用 /抖音。
                    logger.info(
                        "手动抖音候选未能发送媒体，改为发送原链接 query=%s item=%s",
                        query,
                        discovery_id,
                    )
                comment = _fallback_share_comment(candidate)
                message = _format_manual_douyin_share_message(candidate, comment)
                if not video_forwarded and not note_images_forwarded:
                    message = f"{message}\n原链接：{_text(candidate.get('url'))}"
                await self.ctx.send.text(message, stream_id)
                self._store.mark_shared(discovery_id, stream_id)
                logger.info(
                    "手动抖音搜索分享完成 query=%s item=%s video_forwarded=%s note_images_forwarded=%s",
                    query,
                    discovery_id,
                    video_forwarded,
                    note_images_forwarded,
                )
                return
            await self.ctx.send.text(
                f"「{query}」找到了 {len(results)} 条综合页候选，已按点赞从高到低尝试 {attempted_count} 条；都没能成功完成媒体下载和 QQ 合并转发。",
                stream_id,
            )
        except DouyinSearchAuthenticationError:
            await self._browser.open_login_windows(list(self.config.browser.login_pages))
            await self.ctx.send.text(
                f"「{query}」遇到抖音图形安全验证，已自动打开可见浏览器。完成验证后再发一次 /抖音 {query} 就行。",
                stream_id,
            )
        except DouyinSearchNoResultError as exc:
            await self.ctx.send.text(f"「{query}」这次没有解析到可用的自然搜索结果：{exc}", stream_id)
        except Exception as exc:
            logger.exception("手动抖音搜索失败 query=%s", query)
            await self.ctx.send.text(f"这次抖音搜索没跑顺：{str(exc)[:180]}", stream_id)

    @Command("douyin_surf_browser_login", description="打开抖音冲浪专用 Chrome 档案", pattern=r"^/抖音浏览器登录(?:\s+(?P<url>https?://\S+))?$")
    async def command_browser_login(self, stream_id: str = "", matched_groups: dict[str, str] | None = None, **kwargs: Any):
        del kwargs
        denied = await self._ensure_command_allowed(stream_id)
        if denied is not None:
            return denied
        url = _text((matched_groups or {}).get("url"))
        urls = [url] if url else list(self.config.browser.login_pages)
        self._track_task(self._browser.open_login_windows(urls))
        target_description = "指定页面" if url else "抖音标签页"
        return await self._send_command_reply(
            stream_id,
            f"已打开抖音冲浪专用浏览器的{target_description}。请完成抖音登录；这个档案只由抖音冲浪与分享插件使用。",
        )

def create_plugin() -> DouyinSurfPlugin:
    return DouyinSurfPlugin()
