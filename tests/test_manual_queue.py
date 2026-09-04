"""手动抖音点播的全流程队列测试。"""

from __future__ import annotations

from importlib import import_module, util
from pathlib import Path
from sys import modules
from types import MethodType
from typing import Any
from unittest import IsolatedAsyncioTestCase
import asyncio


def _load_plugin() -> Any:
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
    return import_module(f"{package_name}.plugin")


plugin_module = _load_plugin()


class _FakeSend:
    def __init__(self) -> None:
        self.messages: list[tuple[str, str]] = []

    async def text(self, message: str, stream_id: str) -> bool:
        self.messages.append((message, stream_id))
        return True


class _FakeContext:
    def __init__(self) -> None:
        self.send = _FakeSend()


class ManualDouyinQueueTests(IsolatedAsyncioTestCase):
    """后一条点播必须等待前一条完成全部处理。"""

    async def test_manual_requests_run_in_fifo_order(self) -> None:
        instance = object.__new__(plugin_module.DouyinSurfPlugin)
        instance._ctx = _FakeContext()
        instance._manual_douyin_queue_lock = asyncio.Lock()
        instance._manual_douyin_active_count = 2
        instance._manual_douyin_idle = asyncio.Event()
        first_release = asyncio.Event()
        events: list[str] = []

        async def fake_search(self: Any, query: str, stream_id: str) -> None:
            del self, stream_id
            events.append(f"start:{query}")
            if query == "第一条":
                await first_release.wait()
            events.append(f"end:{query}")

        instance._manual_douyin_search_and_share = MethodType(fake_search, instance)
        first = asyncio.create_task(
            instance._run_manual_douyin_queue_item("第一条", "group", waiting_ahead=0)
        )
        await asyncio.sleep(0)
        second = asyncio.create_task(
            instance._run_manual_douyin_queue_item("第二条", "group", waiting_ahead=1)
        )
        await asyncio.sleep(0)

        self.assertEqual(events, ["start:第一条"])
        first_release.set()
        await asyncio.gather(first, second)

        self.assertEqual(
            events,
            ["start:第一条", "end:第一条", "start:第二条", "end:第二条"],
        )
        self.assertEqual(instance._manual_douyin_active_count, 0)
        self.assertTrue(instance._manual_douyin_idle.is_set())
        self.assertEqual(instance.ctx.send.messages[0][1], "group")
        self.assertIn("轮到「第二条」", instance.ctx.send.messages[0][0])
