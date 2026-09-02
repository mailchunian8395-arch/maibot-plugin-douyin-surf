"""候选模型筛选的 JSON 约束与重试测试。"""

from __future__ import annotations

from importlib import import_module, util
from pathlib import Path
from sys import modules
from typing import Any
from unittest import IsolatedAsyncioTestCase


def _load_surf_engine() -> Any:
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
    return import_module(f"{package_name}.surf_engine")


surf_engine = _load_surf_engine()


class _FakeStore:
    """只实现候选筛选需要的最小存储接口。"""

    def __init__(self) -> None:
        self.curated: list[tuple[int, dict[str, Any]]] = []

    @staticmethod
    def get_discoveries(candidate_ids: list[int]) -> list[dict[str, Any]]:
        return [
            {
                "id": candidate_id,
                "title": "测试作品",
                "snippet": "公开摘要",
                "url": "https://www.douyin.com/video/7412345678901234567",
                "published_at": "2026-09-02",
                "likes": 100,
            }
            for candidate_id in candidate_ids
        ]

    def curate_discovery(self, discovery_id: int, result: dict[str, Any]) -> None:
        self.curated.append((discovery_id, result))


class CurationRetryTests(IsolatedAsyncioTestCase):
    """格式错误必须补一次重试，第二次仍错误才交给退避处理。"""

    async def test_retries_once_when_first_model_response_is_not_candidate_json(self) -> None:
        store = _FakeStore()
        prompts: list[str] = []
        responses = iter(
            (
                {"success": True, "response": "我认为这个作品值得保留。"},
                {"success": True, "response": '{"items":[{"id":1,"keep":true}]}'},
            )
        )

        async def generate(prompt: str, temperature: float, max_tokens: int) -> dict[str, Any]:
            del temperature, max_tokens
            prompts.append(prompt)
            return next(responses)

        curated = await surf_engine.curate_candidates(
            None,
            store,
            [1],
            values="测试底线",
            topics=["测试"],
            model="utils",
            generator=generate,
        )

        self.assertEqual([item["id"] for item in curated], [1])
        self.assertEqual(len(prompts), 2)
        self.assertIn("上一次输出不符合格式", prompts[1])
        self.assertEqual([item[0] for item in store.curated], [1])

    async def test_reports_response_preview_after_second_invalid_response(self) -> None:
        store = _FakeStore()

        async def generate(prompt: str, temperature: float, max_tokens: int) -> dict[str, Any]:
            del prompt, temperature, max_tokens
            return {"success": True, "response": "仍然不是 JSON"}

        with self.assertRaisesRegex(ValueError, "连续两次.*仍然不是 JSON"):
            await surf_engine.curate_candidates(
                None,
                store,
                [1],
                values="测试底线",
                topics=["测试"],
                model="utils",
                generator=generate,
            )
