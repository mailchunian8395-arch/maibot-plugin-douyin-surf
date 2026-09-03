"""抖音页面自然响应解析的稳定性样本测试。"""

from __future__ import annotations

import asyncio

from importlib import import_module, util
from json import dumps
from pathlib import Path
from sys import modules
from typing import Any
from unittest import IsolatedAsyncioTestCase, TestCase


def _load_browser_engine() -> Any:
    """以稳定的包名加载带连字符目录中的插件模块。"""

    package_name = "maibot_plugin_douyin_surf"
    package_root = Path(__file__).resolve().parents[1]
    if package_name not in modules:
        spec = util.spec_from_file_location(
            package_name,
            package_root / "__init__.py",
            submodule_search_locations=[str(package_root)],
        )
        if spec is None or spec.loader is None:
            raise RuntimeError("无法加载抖音冲浪插件测试包")
        package = util.module_from_spec(spec)
        modules[package_name] = package
        spec.loader.exec_module(package)
    return import_module(f"{package_name}.browser_engine")


browser_engine = _load_browser_engine()


def _aweme(
    aweme_id: str,
    *,
    duration: int | str = 30_000,
    description: str = "测试视频",
    **extra: Any,
) -> dict[str, Any]:
    """构造与综合搜索响应一致的最小作品样本。"""

    return {
        "aweme_id": aweme_id,
        "desc": description,
        "video": {"duration": duration},
        **extra,
    }


class DouyinSearchPayloadBasicTests(TestCase):
    """覆盖容易因网页响应字段变化而回归的硬过滤规则。"""

    def test_reads_contextual_ssr_video_ids_without_legacy_aweme_key(self) -> None:
        page_html = (
            '<script>window.__RSC_DATA__={"videoMeta":{"content":"x",'
            '"resource":"7677457669691511651"}}</script>'
        )

        video_ids = browser_engine._douyin_ssr_video_ids(page_html, 5)

        self.assertEqual(video_ids, ["7677457669691511651"])

    def test_reads_duration_from_nested_video_in_milliseconds(self) -> None:
        results = browser_engine._douyin_search_results_from_payload(
            {"data": [{"aweme_info": _aweme("7412345678901234567", duration=179_900)}]},
            keyword="猫咪",
            max_results=5,
            max_video_duration_seconds=180,
        )

        self.assertEqual(len(results), 1)
        self.assertIn("时长：179 秒", results[0]["body"])

    def test_rejects_unknown_and_overlong_video_duration(self) -> None:
        payload = {
            "data": [
                {"aweme_info": _aweme("7412345678901234567", duration=181_000)},
                {"aweme_info": {"aweme_id": "7412345678901234568", "desc": "没有时长"}},
            ]
        }

        results = browser_engine._douyin_search_results_from_payload(
            payload,
            keyword="猫咪",
            max_results=5,
            max_video_duration_seconds=180,
        )

        self.assertEqual(results, [])


class _FakePage:
    def __init__(self, url: str) -> None:
        self.url = url
        self.closed = False

    def is_closed(self) -> bool:
        return self.closed

    async def close(self) -> None:
        self.closed = True

    async def evaluate(self, expression: str) -> str:
        if expression == "navigator.userAgent":
            return "test-user-agent"
        raise AssertionError(f"意外的页面表达式：{expression}")


class _FakeContext:
    def __init__(self, pages: list[_FakePage]) -> None:
        self.pages = pages
        self.new_page_count = 0

    async def new_page(self) -> _FakePage:
        self.new_page_count += 1
        page = _FakePage("about:blank")
        self.pages.append(page)
        return page


class BrowserBlankPageCleanupTests(IsolatedAsyncioTestCase):
    """确保空白页清理不会关闭人工登录或验证页面。"""

    @staticmethod
    def _browser(context: _FakeContext) -> Any:
        browser = object.__new__(browser_engine.DeepBrowser)
        browser._context = context
        browser._recommendation_page = None
        return browser

    async def test_reuses_only_initial_blank_page(self) -> None:
        initial_page = _FakePage("about:blank")
        context = _FakeContext([initial_page])
        browser = self._browser(context)

        page = await browser._open_work_page(context)

        self.assertIs(page, initial_page)
        self.assertEqual(context.new_page_count, 0)

    async def test_cleanup_keeps_nonblank_login_or_verification_page(self) -> None:
        login_page = _FakePage("https://www.douyin.com/passport/web/account/login")
        blank_page = _FakePage("about:blank")
        work_page = _FakePage("https://www.douyin.com/video/7412345678901234567")
        context = _FakeContext([login_page, blank_page, work_page])
        browser = self._browser(context)

        await browser._close_work_page(work_page)

        self.assertTrue(work_page.closed)
        self.assertTrue(blank_page.closed)
        self.assertFalse(login_page.closed)

    async def test_finishing_recommendations_keeps_browser_context_and_login_page(self) -> None:
        login_page = _FakePage("https://www.douyin.com/passport/web/account/login")
        recommendation_page = _FakePage("https://www.douyin.com/?recommend=1")
        blank_page = _FakePage("about:blank")
        context = _FakeContext([login_page, recommendation_page, blank_page])
        browser = self._browser(context)
        browser._recommendation_page = recommendation_page
        browser._lock = asyncio.Lock()

        await browser.close_douyin_recommendations()

        self.assertIs(browser._context, context)
        self.assertIsNone(browser._recommendation_page)
        self.assertTrue(recommendation_page.closed)
        self.assertTrue(blank_page.closed)
        self.assertFalse(login_page.closed)

    async def test_request_headers_rechecks_stale_context_before_opening_page(self) -> None:
        stale_context = _FakeContext([])
        fresh_context = _FakeContext([_FakePage("about:blank")])
        browser = self._browser(stale_context)
        browser._headless = True
        browser._lock = asyncio.Lock()
        browser._allowed = lambda url: url == "https://www.douyin.com/video/7412345678901234567"
        ensure_calls: list[bool] = []

        async def ensure(headless: bool) -> _FakeContext:
            ensure_calls.append(headless)
            browser._context = fresh_context
            return fresh_context

        browser._ensure = ensure

        headers = await browser.request_headers_for("https://www.douyin.com/video/7412345678901234567")

        self.assertEqual(ensure_calls, [True])
        self.assertEqual(headers["User-Agent"], "test-user-agent")
        self.assertEqual(headers["Referer"], "https://www.douyin.com/")


class BrowserClosedErrorTests(TestCase):
    """只对 Playwright 明确的上下文关闭错误启用一次性重试。"""

    def test_recognizes_closed_browser_error(self) -> None:
        error = RuntimeError("BrowserContext.new_page: Target page, context or browser has been closed")

        self.assertTrue(browser_engine._is_closed_browser_error(error))

    def test_does_not_treat_navigation_failure_as_browser_close(self) -> None:
        error = RuntimeError("Page.goto: net::ERR_CONNECTION_TIMED_OUT")

        self.assertFalse(browser_engine._is_closed_browser_error(error))


class DouyinSearchPayloadTests(TestCase):
    """覆盖字符串包装响应、付费内容与互动指标过滤。"""

    def test_rejects_paid_content_from_fields_and_visible_text(self) -> None:
        payload = {
            "data": [
                {"aweme_info": _aweme("7412345678901234567", is_paid=True)},
                {"aweme_info": _aweme("7412345678901234568", description="会员专享教程")},
                {"aweme_info": _aweme("7412345678901234569", payment_info={"price": 1})},
            ]
        }

        results = browser_engine._douyin_search_results_from_payload(payload, keyword="教程", max_results=5)

        self.assertEqual(results, [])

    def test_decodes_json_string_and_respects_note_setting(self) -> None:
        note = _aweme("7412345678901234567", images=[{"url_list": ["https://example.invalid/image"]}])
        payload = {"data": '{"aweme_info": ' + dumps(note, ensure_ascii=False) + "}"}

        hidden_results = browser_engine._douyin_search_results_from_payload(
            payload,
            keyword="旅行",
            max_results=5,
            allow_douyin_notes=False,
        )
        visible_results = browser_engine._douyin_search_results_from_payload(
            payload,
            keyword="旅行",
            max_results=5,
            allow_douyin_notes=True,
        )

        self.assertEqual(hidden_results, [])
        self.assertEqual(visible_results[0]["url"], "https://www.douyin.com/note/7412345678901234567")

    def test_filters_required_metrics_and_excluded_urls(self) -> None:
        payload = {
            "data": [
                {
                    "aweme_info": _aweme(
                        "7412345678901234567",
                        statistics={"digg_count": 120, "comment_count": 8, "collect_count": 3, "share_count": 2},
                    )
                },
                {
                    "aweme_info": _aweme(
                        "7412345678901234568",
                        statistics={"digg_count": 150, "comment_count": 10, "collect_count": 4, "share_count": 2},
                    )
                },
            ]
        }

        results = browser_engine._douyin_search_results_from_payload(
            payload,
            keyword="美食",
            max_results=5,
            min_like_count=100,
            min_comment_count=10,
            min_collect_count=4,
            min_share_count=2,
            excluded_urls={"https://www.douyin.com/video/7412345678901234568"},
        )

        self.assertEqual(results, [])
