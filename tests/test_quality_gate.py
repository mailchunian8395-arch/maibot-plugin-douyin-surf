"""候选发布时间筛选的日期解析测试。"""

from __future__ import annotations

from datetime import datetime
from importlib import import_module, util
from pathlib import Path
from sys import modules
from typing import Any
from unittest import TestCase


def _load_quality_gate() -> Any:
    """以稳定包名加载带连字符目录中的质量筛选模块。"""

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
    return import_module(f"{package_name}.quality_gate")


quality_gate = _load_quality_gate()


class PublishAgeFilterTests(TestCase):
    """候选最远天数必须同时覆盖接口时间和页面可见时间。"""

    now = datetime(2026, 9, 3, 18, 0, 0)

    def test_zero_disables_publish_age_filter(self) -> None:
        self.assertTrue(quality_gate.published_within_days("发布日期未知", 0, self.now))

    def test_keeps_relative_dates_within_limit(self) -> None:
        for value in ("刚刚", "22小时前", "昨天", "5天前", "2026-08-04T12:00:00+08:00"):
            with self.subTest(value=value):
                self.assertTrue(quality_gate.published_within_days(value, 30, self.now))

    def test_rejects_stale_and_unknown_dates_when_limit_is_enabled(self) -> None:
        for value in ("31天前", "2026年07月31日", "发布日期未知"):
            with self.subTest(value=value):
                self.assertFalse(quality_gate.published_within_days(value, 30, self.now))

    def test_extracts_visible_relative_publish_dates(self) -> None:
        self.assertEqual(quality_gate.visible_date("@作者 · 5天前"), "5天前")
        self.assertEqual(quality_gate.visible_date("2025年2月7日"), "2025-02-07")
