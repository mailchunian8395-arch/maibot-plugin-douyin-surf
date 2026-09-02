from __future__ import annotations

import json
import random
import re
from typing import Any, Awaitable, Callable

from .storage import LifeStore

_JSON_FENCE_RE = re.compile(r"```(?:json)?\s*(.*?)\s*```", re.DOTALL | re.IGNORECASE)


def split_source_query(raw: str) -> tuple[str, str]:
    """拆分“来源|关键词”配置；缺省来源时按抖音处理。"""
    value = str(raw or "").strip()
    if "|" not in value:
        return "抖音", value
    source, query = value.split("|", 1)
    return source.strip() or "抖音", query.strip()


def select_surf_queries(queries: list[str], count: int, *_: Any) -> list[str]:
    """从已配置标签中均匀抽取本轮抖音搜索关键词。"""
    pool = [str(item).strip() for item in queries if str(item).strip()]
    return random.sample(pool, min(max(1, int(count)), len(pool))) if pool else []


def _parse_json(text: str, opening: str, closing: str, expected: type) -> Any | None:
    value = str(text or "").strip()
    candidates = [value]
    candidates.extend(match.group(1).strip() for match in _JSON_FENCE_RE.finditer(value))
    start, end = value.find(opening), value.rfind(closing)
    if 0 <= start < end:
        candidates.append(value[start : end + 1])
    for candidate in candidates:
        try:
            parsed = json.loads(candidate)
        except (json.JSONDecodeError, TypeError, ValueError):
            continue
        if isinstance(parsed, expected):
            return parsed
    return None


def parse_json_object(text: str) -> dict[str, Any] | None:
    return _parse_json(text, "{", "}", dict)


def parse_json_array(text: str) -> list[Any] | None:
    return _parse_json(text, "[", "]", list)


def llm_text(result: Any) -> str:
    """兼容 MaiBot 与常见 OpenAI 风格模型返回结构。"""
    if isinstance(result, str):
        return result.strip()
    if not isinstance(result, dict):
        return ""
    if isinstance(result.get("items"), list):
        return json.dumps(result, ensure_ascii=False)

    def extract(value: Any, depth: int = 0) -> str:
        if depth > 6 or value is None:
            return ""
        if isinstance(value, str):
            return value.strip()
        if isinstance(value, dict):
            for key in ("output_text", "response", "content", "text", "output"):
                found = extract(value.get(key), depth + 1)
                if found:
                    return found
        if isinstance(value, list):
            return "\n".join(filter(None, (extract(item, depth + 1) for item in value))).strip()
        return ""

    return extract(result)


async def generate_background(ctx: Any, *, prompt: str, model: str, temperature: float, max_tokens: int) -> dict[str, Any]:
    result = await ctx.call_capability(
        "llm.generate", timeout_ms=300_000, prompt=prompt, model=model,
        temperature=temperature, max_tokens=max_tokens,
    )
    if not isinstance(result, dict):
        raise RuntimeError("后台模型调用返回了无法识别的结果")
    if result.get("success") is False:
        raise RuntimeError(f"后台模型调用失败：{result.get('error') or result.get('message') or '未知错误'}")
    return result


def build_curation_prompt(candidates: list[dict[str, Any]], *, tags: list[str], manual_douyin: bool) -> str:
    """构造短小、无角色设定的候选筛选提示词。"""
    payload = [{key: item.get(key, "") for key in ("id", "title", "snippet", "url", "published_at", "likes")} for item in candidates]
    mode = "用户主动搜索：按相关度排序，可保留优质结果。" if manual_douyin else "自动分享：只保留适合主动转发的优质、非重复内容。"
    return (
        "你是抖音内容筛选器。只依据提供的标题、摘要和数据判断，不补充不存在的事实。\n"
        f"标签：{json.dumps(tags, ensure_ascii=False)}\n模式：{mode}\n候选：{json.dumps(payload, ensure_ascii=False)}\n"
        "逐项输出 JSON：{\"items\":[{\"id\":1,\"keep\":true,\"topic\":\"简短主题\",\"summary\":\"不超过50字\",\"reasons\":[\"理由\"],\"confidence\":0.7,\"share_score\":0.8,\"content_quality_score\":0.8,\"heat_score\":0.5,\"share_worthy\":true,\"share_intent\":\"分享角度\",\"risk_label\":\"community\"}]}。\n"
        "过滤广告、引流、标题党、明显不安全、低信息量和与标签无关内容；摘要证据不足时降低分数。只输出 JSON。"
    )


def _invalid_curation_response_preview(result: Any) -> str:
    """为日志提供有限的模型输出摘要，避免非 JSON 结果长期不可诊断。"""

    text = llm_text(result).replace("\n", " ").strip()
    if not text:
        return "<空响应>"
    return text[:300]


async def curate_candidates(
    ctx: Any, store: LifeStore, candidate_ids: list[int], *, values: str,
    topics: list[str], model: str, generator: Callable[[str, float, int], Awaitable[dict[str, Any]]] | None = None, manual_douyin: bool = False,
    max_tokens: int | None = None,
) -> list[dict[str, Any]]:
    """调用单个用户选择的模型筛选候选。"""
    candidates = store.get_discoveries(candidate_ids)
    if not candidates:
        return []
    del values
    prompt = build_curation_prompt(candidates, tags=topics, manual_douyin=manual_douyin)
    budget = max(1024, int(max_tokens or (1400 if manual_douyin else 2200)))
    generate = generator or (
        lambda request_prompt, request_temperature, request_max_tokens: generate_background(
            ctx,
            prompt=request_prompt,
            model=model,
            temperature=request_temperature,
            max_tokens=request_max_tokens,
        )
    )
    result = await generate(prompt, 0.2, budget)
    parsed = parse_json_object(llm_text(result))
    if not isinstance(parsed.get("items") if parsed else None, list):
        # 个别模型会在成功返回后夹带解释、Markdown 或半截文本。它不是候选
        # 内容判断失败，因此只追加一次格式修复重试；仍失败则完整暴露给上层退避。
        retry_prompt = (
            f"{prompt}\n\n"
            "上一次输出不符合格式。本次必须直接输出一个完整 JSON 对象，"
            "首字符必须是 {，末字符必须是 }，且对象必须包含 items 数组；"
            "不要输出 Markdown、解释、思考过程或任何额外文字。"
        )
        result = await generate(retry_prompt, 0.0, budget)
        parsed = parse_json_object(llm_text(result))
    raw_items = parsed.get("items") if parsed else None
    if not isinstance(raw_items, list):
        raise ValueError(
            "冲浪筛选模型连续两次没有返回包含 items 数组的合法候选 JSON；"
            f"第二次输出摘要：{_invalid_curation_response_preview(result)!r}"
        )
    allowed_ids = {int(item["id"]) for item in candidates}
    curated: list[dict[str, Any]] = []
    for raw in raw_items:
        if not isinstance(raw, dict):
            continue
        try:
            discovery_id = int(raw.get("id"))
        except (TypeError, ValueError):
            continue
        if discovery_id not in allowed_ids:
            continue
        if manual_douyin:
            raw["keep"] = True
        store.curate_discovery(discovery_id, raw)
        raw["id"] = discovery_id
        curated.append(raw)
    return curated
