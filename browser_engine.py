from __future__ import annotations

import asyncio
import base64
import logging
import random
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from urllib.parse import quote, quote_plus, unquote, urljoin, urlparse

from .quality_gate import visible_date

logger = logging.getLogger(__name__)

_DOUYIN_PROMOTION_PATTERN = re.compile(
    r"(?:广告投放|广告推广|商业推广|dou\+|品牌推广|赞助内容)",
    re.IGNORECASE,
)
_DOUYIN_AUTH_MARKERS = (
    "扫码登录",
    "登录后即可",
    "登录后即可搜索更多精彩视频",
    "验证码",
    "安全验证",
    "请完成下列验证后继续",
    "请选择所有符合上述描述的图片",
    "拖拽到这里",
)
_DOUYIN_LIKE_PATTERN = re.compile(
    r"(?P<count>\d+(?:\.\d+)?)\s*(?P<unit>万|w)?\s*(?:个?赞|点赞)",
    re.IGNORECASE,
)
_DOUYIN_SSR_AWEME_ID_PATTERN = re.compile(
    r"(?:\\?[\"'](?:aweme_id|awemeId|item_id|itemId|modal_id|modalId)\\?[\"']\s*[:=]\s*\\?[\"']?)(\d{15,22})"
)


class DouyinSearchAuthenticationError(RuntimeError):
    """抖音页面明确要求登录或人工安全验证。"""

    def __init__(self, message: str, *, url: str = "") -> None:
        super().__init__(message)
        self.url = str(url or "").strip()


class DouyinSearchNoResultError(RuntimeError):
    """搜索页可访问，但本轮没有解析出可用的自然作品。"""


def _douyin_requires_authentication(page_text: str) -> bool:
    """只在页面出现明确提示时才将失败归因为登录态或安全验证。"""

    return any(marker in str(page_text or "") for marker in _DOUYIN_AUTH_MARKERS)


def _extract_readable_text(html: str, url: str, fallback: str) -> str:
    """Extract article text from rendered HTML, retaining the DOM text as a safe fallback."""
    clean_html = re.sub(
        r"<(?:nav|header|footer|aside|script|style)\b[^>]*>.*?</(?:nav|header|footer|aside|script|style)>",
        " ",
        str(html or ""),
        flags=re.IGNORECASE | re.DOTALL,
    )
    try:
        from trafilatura import extract

        readable = extract(
            clean_html,
            url=url,
            output_format="txt",
            include_comments=True,
            include_tables=False,
            favor_recall=True,
        )
    except Exception:
        return str(fallback or "").strip()
    clean = str(readable or "").strip()
    for marker in ("\n开发者：", "\nCopyright ©", "\n本网络游戏适合年满"):
        marker_index = clean.find(marker)
        if marker_index >= max(40, len(clean) // 3):
            clean = clean[:marker_index].rstrip()
            break
    return clean if len(clean) >= 40 else str(fallback or "").strip()


def native_targets_for_direction(entries: list[str], direction: str) -> list[str]:
    """Resolve editable ``direction|target1,target2`` native-site routes."""
    clean_direction = str(direction or "").strip().lower()
    for raw_entry in entries:
        raw_name, separator, raw_targets = str(raw_entry or "").partition("|")
        if separator and raw_name.strip().lower() == clean_direction:
            return [item.strip() for item in raw_targets.split(",") if item.strip()]
    return []


def douyin_video_id(href: str) -> str:
    """从抖音搜索结果的直链或弹窗链接中提取作品 ID。"""
    clean_href = str(href or "").strip()
    for pattern in (r"/video/(\d+)", r"[?&](?:modal_id|aweme_id)=(\d+)"):
        match = re.search(pattern, clean_href)
        if match:
            return match.group(1)
    return ""


def douyin_content_url(href: str) -> str:
    """将搜索结果链接规范化为抖音视频或图文作品直链。"""

    clean_href = str(href or "").strip()
    note_match = re.search(r"/note/(\d+)", clean_href)
    if note_match:
        return f"https://www.douyin.com/note/{note_match.group(1)}"
    video_id = douyin_video_id(clean_href)
    return f"https://www.douyin.com/video/{video_id}" if video_id else ""


def _is_douyin_search_response_url(url: str) -> bool:
    """判断是否为抖音页面自然触发的搜索响应。

    抖音的综合搜索接口会随灰度和关键词切换，不能只监听
    ``general/search/single``。这里不主动构造任何接口请求，只收集用户已登录
    页面在提交搜索后自然发出的、同站点的搜索响应，再由载荷解析器确认其中是否
    真正包含作品。
    """

    parsed = urlparse(str(url or ""))
    hostname = (parsed.hostname or "").lower()
    path = parsed.path.lower()
    return hostname.endswith("douyin.com") and "/search/" in path


def _is_douyin_recommendation_response_url(url: str) -> bool:
    """仅接受推荐页自然触发的作品流响应，不构造平台接口请求。"""

    parsed = urlparse(str(url or ""))
    hostname = (parsed.hostname or "").lower()
    path = parsed.path.lower()
    return hostname.endswith("douyin.com") and ("/feed/" in path or "recommend" in path)


def _douyin_ssr_video_ids(page_html: str, max_results: int) -> list[str]:
    """从抖音 PC 搜索页的 SSR/RSC 脚本中提取实际作品 ID。

    部分抖音搜索页不会把作品渲染为可直接读取的 ``a`` 链接，同时搜索接口也
    可能被 Service Worker 包装。已加载页面的 HTML 仍会保留 ``awemeId`` 等
    数据字段，读取这些字段只是在浏览器展示结果中做兜底，不会额外请求接口。
    """

    result: list[str] = []
    seen_ids: set[str] = set()
    for match in _DOUYIN_SSR_AWEME_ID_PATTERN.finditer(str(page_html or "")):
        video_id = match.group(1)
        if video_id in seen_ids:
            continue
        result.append(video_id)
        seen_ids.add(video_id)
        if len(result) >= max(1, int(max_results)):
            break
    return result


def _manual_douyin_link_candidate(
    *,
    href: str,
    link_text: str,
    link_title: str,
    keyword: str,
    search_context_confirmed: bool = False,
) -> dict[str, Any] | None:
    """把手动点播页中的真实作品链接转成低信息候选。

    这条路径只供 ``/抖音`` 使用。手动点播的候选之后仍会深读、评分和安全审核，
    因而不应因抖音结果卡的视觉 DOM 改版而丢弃已经出现的真实作品链接。
    """

    content_url = douyin_content_url(href)
    if not content_url:
        return None
    text = str(link_text or "").strip()
    title = str(link_title or "").strip()
    if _is_douyin_promotion_text(f"{title}\n{text}"):
        return None
    visible_text = f"{title}\n{text}"
    decoded_href = unquote(str(href or ""))
    is_scoped_search_link = "/search/" in decoded_href and keyword in decoded_href
    # 抖音结果卡会把搜索词放在 URL 中、却不重复显示在卡片标题里。只要链接
    # 仍明确属于本次搜索页，就可留给后续深读核验；个人主页和推荐流链接没有
    # 这个搜索路径，仍不会被当作搜索结果。
    if keyword not in visible_text and not is_scoped_search_link and not search_context_confirmed:
        return None
    if not title:
        title = next((line.strip() for line in text.splitlines() if len(line.strip()) >= 4), "")
    return {
        "title": (title or f"抖音 {keyword} 作品")[:500],
        "url": content_url,
        "body": f"搜索标签：{keyword}\n来源：抖音搜索页作品链接\n{text}"[:4000],
        "date": visible_date(text),
    }


def _is_douyin_promoted_aweme(raw_aweme: dict[str, Any]) -> bool:
    """识别搜索响应中明确标注的推广作品。"""

    return any(
        bool(raw_aweme.get(key))
        for key in (
            "is_ads",
            "is_ad",
            "isAd",
            "is_advertisement",
            "is_commerce",
            "ad_info",
            "adInfo",
            "commerce_info",
            "commerceInfo",
        )
    )


def _is_douyin_live_aweme(raw_aweme: dict[str, Any]) -> bool:
    """排除直播间及其预告卡，推荐流补货只收录可独立分享的作品。"""

    return any(
        bool(raw_aweme.get(key))
        for key in (
            "is_live",
            "isLive",
            "live_info",
            "liveInfo",
            "live_room",
            "liveRoom",
            "room_id",
            "roomId",
        )
    )


def _is_douyin_promotion_text(value: str) -> bool:
    return bool(_DOUYIN_PROMOTION_PATTERN.search(str(value or "")))


def _douyin_like_count(raw_aweme: dict[str, Any]) -> int | None:
    """从抖音页面自身响应中读取点赞量；未知数据不能冒充高热内容。"""

    statistics = raw_aweme.get("statistics")
    statistics = statistics if isinstance(statistics, dict) else {}
    for raw_value in (
        statistics.get("digg_count"),
        statistics.get("diggCount"),
        statistics.get("like_count"),
        statistics.get("likeCount"),
        raw_aweme.get("digg_count"),
        raw_aweme.get("diggCount"),
        raw_aweme.get("like_count"),
        raw_aweme.get("likeCount"),
    ):
        if isinstance(raw_value, bool):
            continue
        if isinstance(raw_value, (int, float)):
            return max(0, int(raw_value))
        if isinstance(raw_value, str) and raw_value.strip().isdigit():
            return max(0, int(raw_value.strip()))
    return None


def _douyin_statistic_count(raw_aweme: dict[str, Any], *keys: str) -> int | None:
    """从抖音作品统计字段读取指定互动数；缺失时返回 None。"""

    statistics = raw_aweme.get("statistics")
    statistics = statistics if isinstance(statistics, dict) else {}
    for key in keys:
        for raw_value in (statistics.get(key), raw_aweme.get(key)):
            if isinstance(raw_value, bool):
                continue
            if isinstance(raw_value, (int, float)):
                return max(0, int(raw_value))
            if isinstance(raw_value, str) and raw_value.strip().isdigit():
                return max(0, int(raw_value.strip()))
    return None


def _douyin_duration_seconds(raw_aweme: dict[str, Any]) -> int | None:
    """读取抖音作品时长；网页响应通常以毫秒返回，兼容秒数的字段。"""

    for key in ("duration", "duration_ms", "durationMs", "duration_seconds", "durationSeconds"):
        raw_value = raw_aweme.get(key)
        if isinstance(raw_value, bool):
            continue
        if isinstance(raw_value, (int, float)):
            value = float(raw_value)
        elif isinstance(raw_value, str):
            try:
                value = float(raw_value.strip())
            except ValueError:
                continue
        else:
            continue
        if value <= 0:
            continue
        # 抖音 aweme 的 duration 常见单位为毫秒；秒数字段不会达到千级。
        return max(1, int(value / 1000)) if value >= 1000 else max(1, int(value))
    return None


def _douyin_like_count_from_text(card_text: str) -> int | None:
    """兼容 API 响应不可用时的页面卡片点赞文本。"""

    match = _DOUYIN_LIKE_PATTERN.search(str(card_text or ""))
    if match is None:
        return None
    count = float(match.group("count"))
    if match.group("unit").lower() in {"万", "w"}:
        count *= 10_000
    return max(0, int(count))


def _douyin_search_results_from_payload(
    payload: Any,
    *,
    keyword: str,
    max_results: int,
    min_like_count: int = 0,
    min_comment_count: int = 0,
    min_collect_count: int = 0,
    min_share_count: int = 0,
    max_video_duration_seconds: int = 0,
    excluded_urls: set[str] | None = None,
) -> list[dict[str, Any]]:
    """从抖音页面自身的搜索响应提取作品，避免依赖易变化的结果页 DOM。"""

    extracted: list[dict[str, Any]] = []
    seen_ids: set[str] = set()
    excluded = {str(url).rstrip("/") for url in (excluded_urls or set()) if str(url).strip()}

    def add_aweme(raw_aweme: Any) -> None:
        if not isinstance(raw_aweme, dict) or len(extracted) >= max(1, int(max_results)):
            return
        if _is_douyin_promoted_aweme(raw_aweme):
            return
        if _is_douyin_live_aweme(raw_aweme):
            return
        is_note = bool(raw_aweme.get("images") or raw_aweme.get("image_post_info") or raw_aweme.get("imagePostInfo"))
        duration = _douyin_duration_seconds(raw_aweme)
        if not is_note and max_video_duration_seconds > 0 and (
            duration is None or duration > max_video_duration_seconds
        ):
            return
        like_count = _douyin_like_count(raw_aweme)
        # 自动冲浪以热度为门槛时，无法读取点赞数的作品不能混入高赞候选；
        # 群友手动 /抖音 点播传入 0 时，则保留页面实际搜索到的自然作品。
        if like_count is None and min_like_count > 0:
            return
        if like_count is not None and like_count < max(0, int(min_like_count)):
            return
        comment_count = _douyin_statistic_count(raw_aweme, "comment_count", "commentCount")
        collect_count = _douyin_statistic_count(raw_aweme, "collect_count", "collectCount")
        share_count = _douyin_statistic_count(raw_aweme, "share_count", "shareCount", "forward_count", "forwardCount")
        required_metrics = (
            (comment_count, min_comment_count),
            (collect_count, min_collect_count),
            (share_count, min_share_count),
        )
        if any(count is None and threshold > 0 for count, threshold in required_metrics):
            return
        if any(count is not None and count < max(0, int(threshold)) for count, threshold in required_metrics):
            return
        aweme_id = str(
            raw_aweme.get("aweme_id")
            or raw_aweme.get("awemeId")
            or raw_aweme.get("aweme_id_str")
            or raw_aweme.get("awemeIdStr")
            or ""
        ).strip()
        if not aweme_id.isdigit() or aweme_id in seen_ids:
            return
        author = raw_aweme.get("author") or raw_aweme.get("authorInfo")
        author_name = str(author.get("nickname") or "") if isinstance(author, dict) else ""
        description = str(
            raw_aweme.get("desc") or raw_aweme.get("descText") or raw_aweme.get("title") or ""
        ).strip()
        title = description.splitlines()[0].strip() if description else ""
        if _is_douyin_promotion_text(title):
            return
        if not title:
            title = f"抖音 {keyword} 视频"
        created_at = raw_aweme.get("create_time") or raw_aweme.get("createTime")
        date = ""
        if isinstance(created_at, (int, float)):
            date = datetime.fromtimestamp(created_at, tz=timezone.utc).astimezone().isoformat()
        body_parts = [f"搜索标签：{keyword}"]
        if author_name:
            body_parts.append(f"作者：{author_name}")
        if description:
            body_parts.append(description)
        body_parts.append(f"点赞：{like_count if like_count is not None else '未读取'}")
        body_parts.append(f"评论：{comment_count if comment_count is not None else '未读取'}")
        body_parts.append(f"收藏：{collect_count if collect_count is not None else '未读取'}")
        body_parts.append(f"转发：{share_count if share_count is not None else '未读取'}")
        body_parts.append(f"时长：{duration if duration is not None else '未读取'} 秒")
        content_url = f"https://www.douyin.com/{'note' if is_note else 'video'}/{aweme_id}"
        if content_url.rstrip("/") in excluded:
            seen_ids.add(aweme_id)
            return
        extracted.append(
            {
                "title": title[:500],
                "url": content_url,
                "body": "\n".join(body_parts)[:4000],
                "date": date,
            }
        )
        seen_ids.add(aweme_id)

    def visit(value: Any) -> None:
        if len(extracted) >= max(1, int(max_results)):
            return
        if isinstance(value, list):
            for item in value:
                visit(item)
            return
        if not isinstance(value, dict):
            return
        aweme = value.get("aweme_info") or value.get("awemeInfo") or value.get("aweme")
        if isinstance(aweme, dict):
            add_aweme(aweme)
        elif any(key in value for key in ("aweme_id", "awemeId", "aweme_id_str", "awemeIdStr")):
            add_aweme(value)
        for child in value.values():
            if isinstance(child, (dict, list)):
                visit(child)

    visit(payload)
    return extracted


class DeepBrowser:
    """Dedicated persistent Chrome profile for autonomous, logged-in reading."""

    def __init__(self, profile_dir: Path, allowed_domains: list[str], timeout_seconds: int = 45) -> None:
        self.profile_dir = profile_dir
        self.profile_dir.mkdir(parents=True, exist_ok=True)
        self.allowed_domains = [item.lower().strip() for item in allowed_domains if item.strip()]
        self.timeout_ms = max(5, int(timeout_seconds)) * 1000
        self._playwright: Any = None
        self._context: Any = None
        self._headless: bool | None = None
        self._recommendation_page: Any = None
        self._lock = asyncio.Lock()

    def _allowed(self, url: str) -> bool:
        host = (urlparse(url).hostname or "").lower()
        return any(host == domain or host.endswith("." + domain) for domain in self.allowed_domains)

    async def _ensure(self, headless: bool) -> Any:
        if self._context is not None and self._headless == headless:
            try:
                has_open_page = any(not bool(page.is_closed()) for page in self._context.pages)
            except Exception:
                has_open_page = False
            if has_open_page:
                return self._context
            # 用户可以直接关闭可见 Chrome 窗口。此时 Playwright 仍保留旧的
            # BrowserContext Python 对象，但已经不能 new_page；必须先显式重建。
            logger.info("检测到插件专用浏览器已由外部关闭，正在重新创建浏览器上下文")
        await self.close()
        from playwright.async_api import async_playwright

        self._playwright = await async_playwright().start()
        chrome_path = Path(r"C:\Program Files\Google\Chrome\Application\chrome.exe")
        launch_args: dict[str, Any] = {
            "user_data_dir": str(self.profile_dir),
            "headless": headless,
            "args": ["--disable-blink-features=AutomationControlled"],
        }
        if headless:
            # 无头浏览保持稳定尺寸，避免站点响应式布局影响页面解析。
            launch_args["viewport"] = {"width": 1440, "height": 1000}
        else:
            # 可见浏览器必须使用实际桌面窗口；固定 CSS 视口会让抖音错误进入
            # 窄屏播放器布局，导致推荐流侧栏和操作区看起来没有显示完整。
            launch_args["no_viewport"] = True
            launch_args["args"].append("--start-maximized")
        if chrome_path.is_file():
            launch_args["executable_path"] = str(chrome_path)
        self._context = await self._playwright.chromium.launch_persistent_context(**launch_args)
        self._headless = headless
        return self._context

    async def close(self) -> None:
        self._recommendation_page = None
        if self._context is not None:
            await self._context.close()
            self._context = None
        if self._playwright is not None:
            await self._playwright.stop()
            self._playwright = None
        self._headless = None

    async def douyin_authentication_pending(self) -> bool:
        """检查用户正在操作的可见抖音窗口是否仍显示登录或验证提示。

        此方法绝不调用 ``_ensure``：等待人工登录时若重新启动无头上下文，
        Playwright 会关闭现有可见窗口，导致手机号验证流程中断。
        """

        async with self._lock:
            if self._context is None or self._headless is not False:
                return True
            try:
                douyin_pages = [
                    page
                    for page in self._context.pages
                    if not bool(page.is_closed())
                    and "douyin.com" in (urlparse(str(page.url or "")).hostname or "").lower()
                ]
            except Exception:
                return True
            if not douyin_pages:
                return True
            for page in douyin_pages:
                try:
                    body_text = str(
                        await page.locator("body").inner_text(timeout=self.timeout_ms) or ""
                    )
                except Exception:
                    return True
                if _douyin_requires_authentication(body_text):
                    return True
            return False

    async def close_douyin_recommendations(self) -> None:
        """结束连续刷推荐任务时关闭专用浏览器，清理所有残留标签。"""

        async with self._lock:
            await self.close()

    async def cookies_for(self, url: str) -> list[dict[str, Any]]:
        """Return logged-in cookies for a permitted URL without exposing them to logs."""
        if not self._allowed(url):
            return []
        async with self._lock:
            context = self._context
            if context is None:
                context = await self._ensure(True)
            try:
                cookies = await context.cookies([url])
            except Exception:
                return []
        return [dict(item) for item in cookies if isinstance(item, dict)]

    async def cookie_header_for(self, url: str) -> str:
        """Compatibility projection for callers that only support a Cookie header."""
        cookies = await self.cookies_for(url)
        return "; ".join(
            f"{item.get('name')}={item.get('value')}"
            for item in cookies
            if item.get("name") and item.get("value")
        )

    async def request_headers_for(self, url: str) -> dict[str, str]:
        """返回与登录浏览器一致的最小请求头，供同会话的下载器使用。"""
        if not self._allowed(url):
            return {}
        async with self._lock:
            context = self._context
            if context is None:
                context = await self._ensure(True)
            page = await context.new_page()
            try:
                user_agent = await page.evaluate("navigator.userAgent")
            finally:
                await page.close()
        if not isinstance(user_agent, str) or not user_agent.strip():
            return {}
        host = urlparse(url).scheme + "://" + (urlparse(url).netloc or "www.douyin.com")
        return {
            "User-Agent": user_agent,
            "Referer": host + "/",
        }

    async def _settle_dynamic_page(
        self,
        page: Any,
        *,
        scroll_rounds: int = 4,
        step: int = 1000,
        pause_ms: int = 450,
    ) -> None:
        """Scroll until a dynamic page stops growing, with a strict upper bound."""
        try:
            await page.wait_for_load_state("networkidle", timeout=min(self.timeout_ms, 5000))
        except Exception:
            pass
        previous: tuple[int, int] | None = None
        stable_rounds = 0
        for _ in range(max(1, min(12, int(scroll_rounds)))):
            metrics = await page.evaluate(
                "() => [document.documentElement.scrollHeight, document.querySelectorAll('a, img, video').length]"
            )
            current = (int(metrics[0]), int(metrics[1]))
            if current == previous:
                stable_rounds += 1
            else:
                stable_rounds = 0
            if stable_rounds >= 2:
                break
            previous = current
            await page.mouse.wheel(0, int(step))
            await page.wait_for_timeout(max(100, int(pause_ms)))

    async def _fetch_linked_image(self, page: Any, image_url: str, referer: str) -> str:
        """Fetch an image selected from the rendered page, preserving its original pixels."""
        clean_url = str(image_url or "").strip()
        if clean_url.startswith("//"):
            clean_url = "https:" + clean_url
        clean_url = urljoin(referer, clean_url)
        if urlparse(clean_url).scheme not in {"http", "https"}:
            return ""
        try:
            response = await page.context.request.get(
                clean_url,
                headers={"Referer": referer},
                timeout=min(self.timeout_ms, 20_000),
            )
            content_type = str(response.headers.get("content-type") or "").lower()
            if not response.ok or not content_type.startswith("image/"):
                return ""
            payload = await response.body()
        except Exception:
            return ""
        if not 1000 <= len(payload) <= 5_000_000:
            return ""
        return base64.b64encode(payload).decode("ascii")

    async def _readable_html(self, page: Any) -> str:
        """Prefer the page's main content island before applying generic extraction."""
        clone_script = """el => {
            const clone = el.cloneNode(true);
            clone.querySelectorAll([
                'nav', 'footer', 'aside', 'script', 'style',
                '[class*="footer" i]', '[class*="antiFraud" i]',
                '[class*="download" i]', '[class*="qrcode" i]', '[class*="qr-code" i]'
            ].join(',')).forEach(node => node.remove());
            return clone.outerHTML;
        }"""
        selectors = (
            "article",
            "main",
            "[role='main']",
            "[class*='article-content']",
            "[class*='news-content']",
            "[class*='detail-content']",
            "[class*='post-content']",
        )
        best_html = ""
        best_score = 0
        for selector in selectors:
            locator = page.locator(selector)
            for index in range(min(await locator.count(), 8)):
                candidate = locator.nth(index)
                try:
                    metrics = await candidate.evaluate(
                        """el => {
                            const text = (el.innerText || '').trim();
                            const linkText = Array.from(el.querySelectorAll('a'))
                                .map(node => (node.innerText || '').trim()).join('');
                            return {textLength: text.length, linkLength: linkText.length};
                        }"""
                    )
                    text_length = int(metrics.get("textLength") or 0)
                    link_length = int(metrics.get("linkLength") or 0)
                    score = text_length - min(text_length, link_length) * 0.7
                    if text_length >= 200 and score > best_score:
                        best_html = str(await candidate.evaluate(clone_script) or "")
                        best_score = score
                except Exception:
                    continue
            if best_html and selector in {"article", "main", "[role='main']"}:
                break
        if best_html:
            return best_html
        return str(await page.locator("html").evaluate(clone_script) or "")

    async def _capture_post_preview(self, page: Any, url: str) -> str:
        """Capture the visible post/article card without producing a full-page wall of text."""
        host = (urlparse(url).hostname or "").lower()
        if "douyin.com" in host:
            selectors = ["video", "[data-e2e='feed-active-video']", "[class*='video-player']", "main"]
        else:
            selectors = ["article", "[class*='content']", "main", "body"]

        target: Any = None
        best_score = 0.0
        for selector in selectors:
            locator = page.locator(selector)
            for index in range(min(await locator.count(), 6)):
                candidate = locator.nth(index)
                try:
                    box = await candidate.bounding_box()
                    text = str(await candidate.inner_text() or "").strip()
                except Exception:
                    continue
                minimum_text = 0 if "douyin.com" in host else 80
                if not box or box["width"] < 260 or box["height"] < 160 or len(text) < minimum_text:
                    continue
                score = min(float(box["height"]), 1200.0) * float(box["width"]) + min(len(text), 5000) * 20
                if score > best_score:
                    target = candidate
                    best_score = score
            if target is not None and selector not in {"main", "body"}:
                break
        if target is None:
            return ""

        await target.scroll_into_view_if_needed(timeout=self.timeout_ms)
        await page.wait_for_timeout(350)
        box = await target.bounding_box()
        if not box:
            return ""
        document_size = await page.evaluate(
            "() => ({width: document.documentElement.scrollWidth, height: document.documentElement.scrollHeight})"
        )
        x = max(0.0, float(box["x"]) - 24)
        y = max(0.0, float(box["y"]) - 24)
        width = min(float(document_size["width"]) - x, float(box["width"]) + 48, 1280.0)
        height = min(float(document_size["height"]) - y, max(360.0, min(float(box["height"]) + 48, 1050.0)))
        if width < 200 or height < 160:
            return ""
        screenshot = await page.screenshot(
            type="jpeg",
            quality=84,
            clip={"x": x, "y": y, "width": width, "height": height},
        )
        return base64.b64encode(screenshot).decode("ascii")

    async def _capture_best_post_image(self, page: Any, url: str) -> str:
        """Capture the largest plausible post image while excluding avatars, icons and QR codes."""
        host = (urlparse(url).hostname or "").lower()
        selectors = ["article img", "main img", "[class*='content'] img"]

        target: Any = None
        target_url = ""
        best_score = 0.0
        rejected_markers = ("avatar", "face", "logo", "icon", "emoji", "qrcode", "qr-code", "badge")
        for selector in selectors:
            images = page.locator(selector)
            for index in range(min(await images.count(), 50)):
                image = images.nth(index)
                try:
                    box = await image.bounding_box()
                    details = await image.evaluate(
                        "(el) => ({src: el.currentSrc || el.dataset.src || el.dataset.original || "
                        "el.getAttribute('data-actualsrc') || el.src || '', alt: el.alt || '', "
                        "title: el.title || '', naturalWidth: el.naturalWidth || 0, "
                        "naturalHeight: el.naturalHeight || 0})"
                    )
                except Exception:
                    continue
                if not box or box["width"] < 180 or box["height"] < 150:
                    continue
                metadata = " ".join(
                    str(details.get(key) or "") for key in ("src", "alt", "title")
                ).lower()
                if any(marker in metadata for marker in rejected_markers):
                    continue
                natural_width = max(float(details.get("naturalWidth") or 0), float(box["width"]))
                natural_height = max(float(details.get("naturalHeight") or 0), float(box["height"]))
                if natural_width < 240 or natural_height < 180:
                    continue
                score = min(natural_width, 2400.0) * min(natural_height, 2400.0)
                if score > best_score:
                    target = image
                    target_url = str(details.get("src") or "").strip()
                    best_score = score
            if target is not None:
                break

        # Article/social pages commonly publish the real cover only in metadata.
        try:
            metadata_url = str(
                await page.locator(
                    'meta[property="og:image"], meta[name="twitter:image"], meta[property="twitter:image"]'
                ).first.get_attribute("content")
                or ""
            ).strip()
        except Exception:
            metadata_url = ""
        if metadata_url and (target is None or best_score < 600_000):
            original = await self._fetch_linked_image(page, metadata_url, url)
            if original:
                return original
        if target is None:
            return ""

        try:
            await target.scroll_into_view_if_needed(timeout=self.timeout_ms)
            await page.wait_for_timeout(500)
            original = await self._fetch_linked_image(page, target_url, url)
            if original:
                return original
            screenshot = await target.screenshot(type="jpeg", quality=88)
        except Exception:
            return ""
        return base64.b64encode(screenshot).decode("ascii")

    async def capture_post_media(self, url: str, *, headless: bool = True) -> dict[str, str]:
        """Capture a post's visual payload, preferring an exact image over a post-card fallback."""
        if not self._allowed(url):
            raise ValueError("页面不在深度浏览域名白名单中")
        async with self._lock:
            context = await self._ensure(headless=headless)
            page = await context.new_page()
            try:
                await page.goto(url, wait_until="domcontentloaded", timeout=self.timeout_ms)
                await page.wait_for_timeout(1200)
                host = (urlparse(page.url).hostname or "").lower()
                is_video = "douyin.com" in host
                if is_video:
                    preview = await self._capture_post_preview(page, page.url)
                    if preview:
                        return {"image_base64": preview, "kind": "post_preview"}
                await self._settle_dynamic_page(page, scroll_rounds=6, step=750, pause_ms=300)
                image = await self._capture_best_post_image(page, page.url)
                if image:
                    return {"image_base64": image, "kind": "post_image"}
                preview = await self._capture_post_preview(page, page.url)
                if preview:
                    return {"image_base64": preview, "kind": "post_preview"}
                return {}
            finally:
                await page.close()

    async def capture_post_preview(self, url: str, *, headless: bool = True) -> str:
        """Capture the visible post/video area for link-plus-preview sharing."""
        if not self._allowed(url):
            raise ValueError("页面不在深度浏览域名白名单中")
        async with self._lock:
            context = await self._ensure(headless=headless)
            page = await context.new_page()
            try:
                await page.goto(url, wait_until="domcontentloaded", timeout=self.timeout_ms)
                await self._settle_dynamic_page(page, scroll_rounds=4, step=700, pause_ms=300)
                return await self._capture_post_preview(page, page.url)
            finally:
                await page.close()

    async def open_login_window(self, url: str = "https://www.douyin.com/?recommend=1") -> None:
        await self.open_login_windows([url])

    async def open_login_windows(self, urls: list[str]) -> None:
        clean_urls = [str(url or "").strip() for url in urls if str(url or "").strip()]
        if not clean_urls:
            raise ValueError("没有配置登录页面")
        if any(not self._allowed(url) for url in clean_urls):
            raise ValueError("登录页面不在允许域名中")
        async with self._lock:
            context = await self._ensure(headless=False)
            first_page: Any = None
            for index, url in enumerate(clean_urls):
                host = (urlparse(url).hostname or "").lower()
                page = next(
                    (
                        existing
                        for existing in context.pages
                        if (urlparse(existing.url).hostname or "").lower() == host
                    ),
                    None,
                )
                if page is None:
                    page = context.pages[0] if index == 0 and len(context.pages) == 1 and context.pages[0].url == "about:blank" else await context.new_page()
                await page.goto(url, wait_until="domcontentloaded", timeout=self.timeout_ms)
                if first_page is None:
                    first_page = page
            if first_page is not None:
                await first_page.bring_to_front()

    async def read_page(self, url: str, *, headless: bool = True, max_chars: int = 30000) -> dict[str, Any]:
        if not self._allowed(url):
            raise ValueError("页面不在深度浏览域名白名单中")
        async with self._lock:
            context = await self._ensure(headless=headless)
            page = await context.new_page()
            try:
                await page.goto(url, wait_until="domcontentloaded", timeout=self.timeout_ms)
                await page.wait_for_timeout(1200)
                await self._settle_dynamic_page(page, scroll_rounds=8, step=900, pause_ms=350)
                title = await page.title()
                text = await page.locator("body").inner_text(timeout=self.timeout_ms)
                host = (urlparse(page.url).hostname or "").lower()
                html = await self._readable_html(page)
                text = await asyncio.to_thread(_extract_readable_text, html, page.url, text)
                images: list[dict[str, Any]] = []
                image_locators = page.locator("img")
                for index in range(min(await image_locators.count(), 40)):
                    image = image_locators.nth(index)
                    try:
                        box = await image.bounding_box()
                        if not box or box["width"] < 120 or box["height"] < 90:
                            continue
                        images.append(
                            {
                                "alt": str(await image.get_attribute("alt") or "").strip()[:300],
                                "title": str(await image.get_attribute("title") or "").strip()[:300],
                                "src": str(
                                    await image.evaluate(
                                        "el => el.currentSrc || el.dataset.src || el.dataset.original || "
                                        "el.getAttribute('data-actualsrc') || el.src || ''"
                                    )
                                    or ""
                                ).strip()[:1000],
                                "width": round(float(box["width"])),
                                "height": round(float(box["height"])),
                            }
                        )
                    except Exception:
                        continue
                return {
                    "url": page.url,
                    "title": title.strip(),
                    "text": text.strip()[: max(1000, int(max_chars))],
                    "images": images,
                    "comments": [],
                }
            finally:
                await page.close()

    async def discover_douyin_recommendations(
        self,
        *,
        max_results: int = 2,
        cards_to_browse: int = 8,
        headless: bool = True,
        min_like_count: int = 0,
        min_comment_count: int = 0,
        min_collect_count: int = 0,
        min_share_count: int = 0,
        max_video_duration_seconds: int = 0,
        excluded_urls: set[str] | None = None,
        keep_open: bool = False,
    ) -> list[dict[str, Any]]:
        """自然浏览已登录抖音推荐流，连同当前推荐页的实际画面返回新作品。"""

        douyin_home_url = "https://www.douyin.com/?recommend=1"
        if not self._allowed(douyin_home_url):
            raise ValueError("抖音推荐页不在深度浏览域名白名单中")
        excluded = {str(url).rstrip("/") for url in (excluded_urls or set()) if str(url).strip()}
        async with self._lock:
            context = await self._ensure(headless=headless)
            page = self._recommendation_page if keep_open else None
            if page is None or bool(page.is_closed()):
                page = await context.new_page()
                if keep_open:
                    self._recommendation_page = page
            keep_visible_page_for_authentication = False
            try:
                feed_responses: list[Any] = []

                def remember_feed_response(response: Any) -> None:
                    if _is_douyin_recommendation_response_url(str(getattr(response, "url", ""))):
                        feed_responses.append(response)

                page.on("response", remember_feed_response)
                response_listener_attached = True
                if str(page.url or "") == "about:blank":
                    # 推荐页由插件专用抖音档案的“默认打开”设置决定。不要再代替
                    # 用户点击侧栏，避免覆盖其站内偏好，也避免让滚轮初始落在侧栏。
                    await page.goto(douyin_home_url, wait_until="domcontentloaded", timeout=self.timeout_ms)
                    await page.wait_for_timeout(2_000)
                    logger.info("已按插件专用抖音档案的默认入口打开推荐流 current_url=%s", page.url)

                body_text = str(await page.locator("body").inner_text(timeout=self.timeout_ms) or "")
                if _douyin_requires_authentication(body_text):
                    keep_visible_page_for_authentication = not headless
                    raise DouyinSearchAuthenticationError(
                        "抖音推荐流出现登录或图形安全验证，请在已打开的浏览器窗口完成验证后重试"
                    )

                results: list[dict[str, Any]] = []
                pending_results: list[dict[str, Any]] = []
                seen_urls: set[str] = set()
                parsed_response_count = 0

                async def collect_recommendations() -> None:
                    """读取本次页面自然返回的推荐作品，等待与当前画面一一对应。"""

                    nonlocal parsed_response_count
                    for response in feed_responses[parsed_response_count:]:
                        if not bool(getattr(response, "ok", False)):
                            continue
                        try:
                            payload = await response.json()
                        except Exception:
                            continue
                        for item in _douyin_search_results_from_payload(
                            payload,
                            keyword="抖音推荐",
                            max_results=max(12, int(max_results) * 6),
                            min_like_count=min_like_count,
                            min_comment_count=min_comment_count,
                            min_collect_count=min_collect_count,
                            min_share_count=min_share_count,
                            max_video_duration_seconds=max_video_duration_seconds,
                            excluded_urls=excluded,
                        ):
                            url = str(item.get("url") or "").rstrip("/")
                            if not url or url in seen_urls:
                                continue
                            item["source"] = "美图·抖音推荐"
                            pending_results.append(item)
                            seen_urls.add(url)
                    parsed_response_count = len(feed_responses)

                async def capture_visible_recommendation() -> bool:
                    """只给已在当前推荐页可见的作品附上页面截图，避免错配作品与画面。"""

                    if not pending_results:
                        return False
                    current_page_text = str(
                        await page.locator("body").inner_text(timeout=self.timeout_ms) or ""
                    )
                    normalized_page_text = re.sub(r"\s+", "", current_page_text)
                    matched_index = -1
                    for index, item in enumerate(pending_results):
                        title = re.sub(r"\s+", "", str(item.get("title") or ""))
                        # 标题通常完整显示在推荐页左下角；至少以 8 个字确认，
                        # 防止把预加载队列中的下一条错误配给当前画面。
                        if len(title) >= 8 and title in normalized_page_text:
                            matched_index = index
                            break
                    if matched_index < 0:
                        return False
                    screenshot = await page.screenshot(type="jpeg", quality=85)
                    item = pending_results.pop(matched_index)
                    item["visual_screenshot_base64"] = base64.b64encode(screenshot).decode("ascii")
                    results.append(item)
                    logger.info(
                        "抖音推荐流已截取当前作品画面 browsed_results=%s title=%s",
                        len(results),
                        str(item.get("title") or "")[:80],
                    )
                    return True

                for index in range(max(1, int(cards_to_browse))):
                    await collect_recommendations()
                    await capture_visible_recommendation()
                    if len(results) >= max(1, int(max_results)):
                        logger.info(
                            "抖音推荐流发现审美候选 browsed_cards=%s results=%s response_count=%s",
                            index + 1,
                            len(results),
                            len(feed_responses),
                        )
                        return results
                    # 鼠标必须先移到中间视频区域，否则滚轮会落在左侧导航栏，
                    # 既不会切换作品，也拿不到下一条可审核的真实画面。
                    viewport = await page.evaluate(
                        "() => ({width: window.innerWidth, height: window.innerHeight})"
                    )
                    viewport_width = max(1, int(viewport.get("width", 0)))
                    viewport_height = max(1, int(viewport.get("height", 0)))
                    await page.mouse.move(viewport_width / 2, viewport_height / 2)
                    # 每次仅划过一屏，模拟正常刷推荐；不批量构造翻页请求。
                    await page.mouse.wheel(0, 900)
                    await page.wait_for_timeout(1_200)
                    body_text = str(await page.locator("body").inner_text(timeout=self.timeout_ms) or "")
                    if _douyin_requires_authentication(body_text):
                        keep_visible_page_for_authentication = not headless
                        raise DouyinSearchAuthenticationError(
                            "抖音推荐流出现登录或图形安全验证，请在已打开的浏览器窗口完成验证后重试"
                        )
                await collect_recommendations()
                await capture_visible_recommendation()
                logger.info(
                    "抖音推荐流本轮浏览结束 browsed_cards=%s results=%s response_count=%s",
                    max(1, int(cards_to_browse)),
                    len(results),
                    len(feed_responses),
                )
                return results
            finally:
                if "response_listener_attached" in locals() and response_listener_attached:
                    page.remove_listener("response", remember_feed_response)
                if keep_open and not keep_visible_page_for_authentication:
                    # 低水位补货会在同一张推荐页上接着向下刷，到库存上限时由
                    # close_douyin_recommendations 统一关闭整个浏览器上下文。
                    pass
                elif not headless:
                    if not keep_visible_page_for_authentication:
                        await self.close()
                else:
                    await page.close()

    async def discover_douyin_search(
        self,
        *,
        keyword: str,
        search_type: str = "general",
        max_results: int = 4,
        scroll_rounds: int = 4,
        headless: bool = True,
        require_scroll_before_return: bool = False,
        minimum_results_before_return: int = 1,
        initial_result_wait_ms: int = 0,
        min_like_count: int = 0,
        min_comment_count: int = 0,
        min_collect_count: int = 0,
        min_share_count: int = 0,
        max_video_duration_seconds: int = 0,
        allow_low_metadata_results: bool = False,
        excluded_urls: set[str] | None = None,
    ) -> list[dict[str, Any]]:
        """浏览已登录的抖音搜索页，返回该页中自然出现的具体作品链接。"""
        if search_type not in {"general", "video"}:
            raise ValueError(f"不支持的抖音搜索页类型：{search_type}")
        excluded = {str(url).rstrip("/") for url in (excluded_urls or set()) if str(url).strip()}
        # 综合页采用网页端当前实际展示作品栅格的精选搜索入口。旧 /search/ 路由
        # 偶尔只会留下永久骨架屏；视频页仍保留原入口，供手动命令无候选时保底。
        # 两者均在插件专用、已登录浏览器上下文中自然打开，不构造或伪造接口签名请求。
        search_path = "jingxuan/search" if search_type == "general" else "search"
        search_url = f"https://www.douyin.com/{search_path}/{quote(keyword, safe='')}?type={search_type}"
        if not self._allowed(search_url):
            raise ValueError("抖音搜索页不在深度浏览域名白名单中")
        async with self._lock:
            context = await self._ensure(headless=headless)
            page = await context.new_page()
            # 仅当抖音明确展示登录或图形验证时，才保留可见页面给用户处理。
            # 其它所有搜索页必须在 finally 中关闭，避免持久化浏览器累积标签。
            keep_visible_page_for_authentication = False
            try:
                # 搜索结果页的锚点和卡片频繁改版；收集该页面已登录会话自然发出的
                # 所有搜索响应，而非押注某一个灰度接口。不自行构造签名请求，也不
                # 绕过抖音的访问控制。
                search_responses: list[Any] = []

                def remember_search_response(response: Any) -> None:
                    response_url = str(getattr(response, "url", ""))
                    # 同一个搜索页会并发拉取推荐、热榜和联想词。仅保留 URL 中明确
                    # 带有本次关键词的自然搜索响应，不能把同站的其它 /search/
                    # 接口误作当前关键词的结果。请求体可能为二进制，读取它会触发
                    # Playwright 的 UTF-8 解码异常，因此这里严格只读取 URL。
                    if _is_douyin_search_response_url(response_url) and keyword in unquote(response_url):
                        search_responses.append(response)

                # 搜索请求往往在结果页导航和懒加载阶段才发出。监听必须在导航前
                # 挂上，并覆盖结果卡等待及滚动稳定阶段；仍然只读取页面自然发出的
                # 响应，不额外调用抖音接口。
                page.on("response", remember_search_response)
                response_listener_attached = True
                await page.goto(search_url, wait_until="domcontentloaded", timeout=self.timeout_ms)
                await page.wait_for_timeout(2500)
                logger.info(
                    "抖音搜索页已打开 keyword=%s search_type=%s current_url=%s",
                    keyword,
                    search_type,
                    str(page.url or ""),
                )
                body_text = str(await page.locator("body").inner_text(timeout=self.timeout_ms) or "")
                if _douyin_requires_authentication(body_text):
                    keep_visible_page_for_authentication = not headless
                    raise DouyinSearchAuthenticationError(
                        "抖音搜索出现登录或图形安全验证，请在已打开的浏览器窗口完成验证后重试",
                        url=str(page.url or ""),
                    )

                async def parse_search_responses() -> tuple[list[dict[str, Any]], list[str]]:
                    """从当前已完成的自然搜索响应中提取满足点赞线的作品。"""

                    api_results: list[dict[str, Any]] = []
                    seen_api_urls: set[str] = set()
                    response_urls: list[str] = []
                    for search_response in search_responses:
                        response_url = str(getattr(search_response, "url", ""))
                        if response_url:
                            response_urls.append(response_url)
                        if not bool(getattr(search_response, "ok", False)):
                            continue
                        try:
                            response_payload = await search_response.json()
                        except Exception:
                            continue
                        response_results = _douyin_search_results_from_payload(
                            response_payload,
                            keyword=keyword,
                            max_results=max_results,
                            min_like_count=min_like_count,
                            min_comment_count=min_comment_count,
                            min_collect_count=min_collect_count,
                            min_share_count=min_share_count,
                            max_video_duration_seconds=max_video_duration_seconds,
                            excluded_urls=excluded,
                        )
                        for item in response_results:
                            url = str(item.get("url") or "")
                            if not url or url in seen_api_urls:
                                continue
                            api_results.append(item)
                            seen_api_urls.add(url)
                            if len(api_results) >= max(1, int(max_results)):
                                return api_results, response_urls
                    return api_results, response_urls

                api_results, response_urls = await parse_search_responses()
                minimum_results = max(1, int(minimum_results_before_return))
                if (
                    len(api_results) >= minimum_results
                    and not require_scroll_before_return
                ):
                    logger.info(
                        "抖音搜索通过页面响应获取作品 keyword=%s results=%s response_count=%s",
                        keyword,
                        len(api_results),
                        len(search_responses),
                    )
                    return api_results
                # 抖音有时会先渲染搜索框、导航或少量推荐链接，真正的搜索响应
                # 稍后才到。手动点播不能因为这几个无效锚点已经出现，就把“尚未
                # 加载完”误报成“没有结果”；自动冲浪保持零等待，以免放慢后台轮次。
                remaining_wait_ms = max(0, int(initial_result_wait_ms))
                while remaining_wait_ms > 0 and len(api_results) < minimum_results:
                    wait_ms = min(1_000, remaining_wait_ms)
                    await page.wait_for_timeout(wait_ms)
                    remaining_wait_ms -= wait_ms
                    api_results, response_urls = await parse_search_responses()
                    if (
                        len(api_results) >= minimum_results
                        and not require_scroll_before_return
                    ):
                        logger.info(
                            "抖音搜索等待动态响应后获取作品 keyword=%s results=%s response_count=%s",
                            keyword,
                            len(api_results),
                            len(search_responses),
                        )
                        return api_results
                if api_results:
                    logger.info(
                        "抖音搜索首屏候选不足，补充下拉候选池 keyword=%s results=%s minimum_results=%s scroll_rounds=%s",
                        keyword,
                        len(api_results),
                        minimum_results,
                        scroll_rounds,
                    )
                # 视频页仍可能通过直链、合集卡或弹窗暴露作品，统一采集后再去重筛选。
                link_selector = (
                    'a[href*="/video/"], '
                    'a[href*="/note/"], '
                    'a[href*="modal_id="], '
                    'a[href*="aweme_id="]'
                )
                links = page.locator(link_selector)
                try:
                    await links.first.wait_for(
                        state="attached",
                        timeout=min(self.timeout_ms, 15_000),
                    )
                except Exception:
                    body_text = str(await page.locator("body").inner_text(timeout=self.timeout_ms) or "")
                    if _douyin_requires_authentication(body_text):
                        keep_visible_page_for_authentication = not headless
                        raise DouyinSearchAuthenticationError(
                            "抖音搜索出现登录或图形安全验证，请在已打开的浏览器窗口完成验证后重试"
                        )
                    # 新版搜索结果首屏不一定以传统锚点出现；不能在这里提前判空，
                    # 继续滚动并等待懒加载后的卡片、搜索响应或 SSR 数据。
                    logger.info(
                        "抖音搜索首屏未出现作品链接，继续等待动态结果 keyword=%s response_count=%s",
                        keyword,
                        len(response_urls),
                    )
                await self._settle_dynamic_page(
                    page, scroll_rounds=scroll_rounds, step=1200, pause_ms=650
                )

                # 结果页稳定后再读取完整的响应集。这里放在滚动之后，能拿到首屏
                # 之后才返回的搜索接口，同时也不会把请求中的半截响应当成空结果。
                api_results, response_urls = await parse_search_responses()
                if api_results:
                    logger.info(
                        "抖音搜索通过页面响应获取作品 keyword=%s results=%s response_count=%s",
                        keyword,
                        len(api_results),
                        len(search_responses),
                    )
                    return api_results

                # 抖音有时先展示搜索骨架，滚动或等待期间才弹出登录遮罩、滑块验证。
                # 最终判空前必须再次检测，才能转为可见窗口并给用户留出完成登录的时间。
                body_text = str(await page.locator("body").inner_text(timeout=self.timeout_ms) or "")
                if _douyin_requires_authentication(body_text):
                    keep_visible_page_for_authentication = not headless
                    raise DouyinSearchAuthenticationError(
                        "抖音搜索出现登录或图形安全验证，请在已打开的浏览器窗口完成验证后重试",
                        url=str(page.url or ""),
                    )

                results: list[dict[str, Any]] = []
                seen_urls: set[str] = set()
                low_metadata_results: list[dict[str, Any]] = []
                seen_low_metadata_urls: set[str] = set()
                current_url = str(page.url or "")
                search_context_confirmed = (
                    keyword in body_text
                    or quote_plus(keyword) in current_url
                    or "/search/" in urlparse(current_url).path.lower()
                )
                for index in range(min(await links.count(), max(24, int(max_results) * 12))):
                    link = links.nth(index)
                    href = str(await link.get_attribute("href") or "").strip()
                    url = douyin_content_url(href)
                    if not url:
                        continue
                    if url in seen_urls:
                        continue
                    if url.rstrip("/") in excluded:
                        continue
                    metadata = await link.evaluate(
                        """el => {
                            const promotionPattern = /广告投放|广告推广|商业推广|dou\\+|品牌推广|赞助内容/i;
                            let node = el;
                            for (let i = 0; i < 6 && node; i++, node = node.parentElement) {
                                const text = (node.innerText || '').trim();
                                const mediaCount = node.querySelectorAll('img, video, [style*="background-image"]').length;
                                const linkCount = node.querySelectorAll('a[href]').length;
                                // 只接受有视觉主体且链接数量有限的结果卡。页面大容器、页脚、
                                // 推荐墙和导航也可能带 modal_id，但不属于单条搜索结果。
                                if (!text || text.length > 6000 || mediaCount < 1 || linkCount > 24) continue;
                                const img = node.querySelector('img');
                                return {
                                    isResultCard: true,
                                    isPromotion: promotionPattern.test(text),
                                    text,
                                    title: el.getAttribute('title') || el.getAttribute('aria-label') ||
                                        (img && (img.alt || img.title)) || ''
                                };
                            }
                            return {isResultCard: false, isPromotion: false, text: '', title: ''};
                        }"""
                    )
                    metadata = metadata if isinstance(metadata, dict) else {}
                    card_text = str(metadata.get("text") or "").strip()
                    title = str(metadata.get("title") or "").strip()
                    if not bool(metadata.get("isResultCard")) or bool(metadata.get("isPromotion")):
                        continue
                    if not title:
                        title = next(
                            (
                                line.strip()
                                for line in card_text.splitlines()
                                if len(line.strip()) >= 4 and not _is_douyin_promotion_text(line)
                            ),
                            "",
                        )
                    if not title or _is_douyin_promotion_text(title):
                        continue
                    if allow_low_metadata_results:
                        # 手动点播不能把搜索页推荐区的 /video/ 链接当作结果。只有
                        # 当前卡片本身明确展示了关键词，才允许进入后续深读与评分。
                        low_metadata_candidate = _manual_douyin_link_candidate(
                            href=href,
                            link_text=card_text,
                            link_title=title,
                            keyword=keyword,
                            # 已确认当前就是本次关键词的搜索页。抖音往往把关键词
                            # 拆散显示在标签、标题和作者信息里，不能再要求每张
                            # 卡片的单独文本完整包含原始输入词，否则会误报空结果。
                            search_context_confirmed=search_context_confirmed,
                        )
                        if low_metadata_candidate is not None and url not in seen_low_metadata_urls:
                            low_metadata_results.append(low_metadata_candidate)
                            seen_low_metadata_urls.add(url)
                        continue
                    like_count = _douyin_like_count_from_text(card_text)
                    if like_count is None and min_like_count > 0:
                        continue
                    if like_count is not None and like_count < max(0, int(min_like_count)):
                        continue
                    results.append(
                        {
                            "title": title[:500],
                            "url": url,
                            "body": f"搜索标签：{keyword}\n点赞：{like_count if like_count is not None else '未读取'}\n{card_text}"[:4000],
                            "date": visible_date(card_text),
                        }
                    )
                    seen_urls.add(url)
                    if len(results) >= max(1, int(max_results)):
                        break
                if not results:
                    if allow_low_metadata_results and low_metadata_results:
                        logger.info(
                            "手动抖音搜索使用低信息作品链接候选 keyword=%s results=%s",
                            keyword,
                            len(low_metadata_results),
                        )
                        return low_metadata_results[: max(1, int(max_results))]
                    # 新版 PC 搜索页有时把结果藏进 React Server Components 数据，
                    # 页面上既没有可用锚点，也不会暴露可解析的 JSON 搜索响应。
                    # 仅在当前页已明确处于本次关键词搜索上下文时，才接受 SSR 中
                    # 的作品 ID，避免把个人主页推荐流误当作搜索结果。手动 /抖音
                    # 也必须走这条路径：新版结果卡经常不再暴露作品锚点，若仅因
                    # 手动模式就禁用 SSR，会把用户眼前可见的搜索结果全部误判为空。
                    page_html = await page.content()
                    query_in_page = keyword in body_text or quote_plus(keyword) in current_url
                    search_page = "/search/" in urlparse(current_url).path.lower()
                    if query_in_page or search_page:
                        ssr_video_ids = _douyin_ssr_video_ids(page_html, max_results)
                        if ssr_video_ids:
                            logger.info(
                                "抖音搜索通过页面 SSR 获取作品 keyword=%s results=%s manual=%s current_url=%s",
                                keyword,
                                len(ssr_video_ids),
                                allow_low_metadata_results,
                                current_url,
                            )
                            return [
                                {
                                    "title": f"抖音 {keyword} 视频",
                                    "url": f"https://www.douyin.com/video/{video_id}",
                                    "body": f"搜索标签：{keyword}\n来源：抖音搜索页 SSR 作品数据",
                                    "date": "",
                                }
                                for video_id in ssr_video_ids
                                if f"https://www.douyin.com/video/{video_id}" not in excluded
                            ]
                    raise DouyinSearchNoResultError(
                        "抖音搜索页没有解析到合格自然作品；"
                        f"搜索响应={len(response_urls)}，候选链接={await links.count()}，"
                        "未检测到登录或安全验证提示"
                    )
                random.shuffle(results)
                return results[: max(1, int(max_results))]
            finally:
                if "response_listener_attached" in locals() and response_listener_attached:
                    page.remove_listener("response", remember_search_response)
                if not headless:
                    if not keep_visible_page_for_authentication:
                        # 可见窗口只用于让用户核验搜索过程；任务结束后关闭整个持久化
                        # 上下文，避免积累搜索页或留下 about:blank 空标签。
                        await self.close()
                else:
                    await page.close()

    async def capture_highlight(
        self,
        url: str,
        *,
        kind: str,
        keyword: str = "",
        headless: bool = True,
    ) -> str:
        """Capture one exact comment card; never fall back to a post or body screenshot."""
        if not self._allowed(url):
            raise ValueError("页面不在深度浏览域名白名单中")
        async with self._lock:
            context = await self._ensure(headless=headless)
            page = await context.new_page()
            try:
                await page.goto(url, wait_until="domcontentloaded", timeout=self.timeout_ms)
                await page.wait_for_timeout(1200)
                target: Any = None
                clean_kind = str(kind or "").strip().lower()
                clean_keyword = str(keyword or "").strip()

                if clean_kind != "comment" or not clean_keyword:
                    return ""

                if clean_kind == "comment" and clean_keyword:
                    fragments = [
                        clean_keyword[:240],
                        clean_keyword[:120],
                        clean_keyword[:80],
                        clean_keyword[:48],
                        clean_keyword[:32],
                        clean_keyword[:24],
                        clean_keyword[:16],
                        clean_keyword[:12],
                    ]
                    matched: Any = None
                    for fragment in dict.fromkeys(item.strip() for item in fragments if len(item.strip()) >= 8):
                        locator = page.get_by_text(fragment, exact=False)
                        if await locator.count():
                            matched = locator.first
                            break
                    if matched is not None:
                        # 截整条评论卡片（作者、点赞、正文和楼中楼），而不是只截文字行。
                        ancestors = matched.locator(
                            "xpath=ancestor::*[contains(translate(@class,'COMMENTREPLY','commentreply'),'comment') "
                            "or contains(translate(@class,'COMMENTREPLY','commentreply'),'reply')][1]"
                        )
                        if await ancestors.count():
                            candidate = ancestors.first
                            try:
                                box = await candidate.bounding_box()
                                candidate_text = str(await candidate.inner_text() or "").strip()
                            except Exception:
                                box = None
                                candidate_text = ""
                            if box and 220 <= box["width"] <= 1300 and 35 <= box["height"] <= 900 and clean_keyword[:24] in candidate_text:
                                target = candidate
                        if target is None:
                            target = matched

                if target is None:
                    return ""
                await target.scroll_into_view_if_needed(timeout=self.timeout_ms)
                await page.wait_for_timeout(500)
                box = await target.bounding_box()
                if not box:
                    return ""
                document_size = await page.evaluate(
                    "() => ({width: document.documentElement.scrollWidth, height: document.documentElement.scrollHeight})"
                )
                margin_x = 16
                margin_y = 8
                x = max(0.0, float(box["x"]) - margin_x)
                y = max(0.0, float(box["y"]) - margin_y)
                width = min(float(document_size["width"]) - x, float(box["width"]) + margin_x * 2)
                height = min(
                    float(document_size["height"]) - y,
                    max(90.0, min(900.0, float(box["height"]) + margin_y * 2)),
                )
                if width < 100 or height < 100:
                    return ""
                screenshot = await page.screenshot(
                    type="jpeg",
                    quality=84,
                    clip={"x": x, "y": y, "width": width, "height": height},
                )
                return base64.b64encode(screenshot).decode("ascii")
            finally:
                await page.close()
