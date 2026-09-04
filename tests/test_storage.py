"""候选库存中手动抖音条目的隔离测试。"""

from __future__ import annotations

from importlib import import_module, util
from pathlib import Path
from sys import modules
from tempfile import TemporaryDirectory
from typing import Any
from unittest import TestCase
import gc


def _load_storage() -> Any:
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
    return import_module(f"{package_name}.storage")


storage = _load_storage()


class PendingCurationTests(TestCase):
    """自动冲浪的 AI 初筛不得接管手动 /抖音 临时结果。"""

    def test_manual_douyin_candidates_do_not_enter_automatic_curation(self) -> None:
        with TemporaryDirectory() as directory:
            store = storage.LifeStore(Path(directory) / "life.db")
            store.add_candidates(
                [
                    {
                        "source": "手动·抖音",
                        "title": "手动搜索结果",
                        "url": "https://www.douyin.com/video/7000000000000000001",
                    },
                    {
                        "source": "抖音·推荐",
                        "title": "自动冲浪结果",
                        "url": "https://www.douyin.com/video/7000000000000000002",
                    },
                ]
            )

            pending = store.pending_curation()
            del store
            gc.collect()

        self.assertEqual([item["title"] for item in pending], ["自动冲浪结果"])
