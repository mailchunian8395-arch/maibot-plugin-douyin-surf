"""插件内 OpenAI 兼容模型直连，仅用于用户主动配置的筛选与视觉模型。"""

from __future__ import annotations

import json
from typing import Any


def _to_openai_messages(prompt: Any) -> list[dict[str, Any]]:
    """将 MaiBot 的图片片段转换为 OpenAI Chat Completions 兼容格式。"""

    if isinstance(prompt, str):
        return [{"role": "user", "content": prompt}]
    if not isinstance(prompt, list):
        raise ValueError("直连模型请求内容格式无效")
    messages: list[dict[str, Any]] = []
    for message in prompt:
        if not isinstance(message, dict):
            continue
        content = message.get("content")
        if not isinstance(content, list):
            messages.append(dict(message))
            continue
        converted: list[dict[str, Any]] = []
        for part in content:
            if isinstance(part, dict) and part.get("type") == "image":
                image_format = str(part.get("image_format") or "jpeg")
                image_base64 = str(part.get("image_base64") or "")
                converted.append({"type": "image_url", "image_url": {"url": f"data:image/{image_format};base64,{image_base64}"}})
            elif isinstance(part, dict):
                converted.append(dict(part))
        messages.append({"role": str(message.get("role") or "user"), "content": converted})
    if not messages:
        raise ValueError("直连模型请求内容为空")
    return messages


async def generate_openai_compatible(config: Any, *, prompt: Any, temperature: float, max_tokens: int) -> dict[str, Any]:
    """调用用户配置的 OpenAI 兼容 API，返回与 MaiBot 能力一致的简化结果。"""

    if not config.api_base_url.strip() or not config.api_key.strip() or not config.model_name.strip():
        raise RuntimeError("已选择插件内 API 直连，请完整填写 API 地址、API 密钥和模型名")
    try:
        extra_body = json.loads(config.extra_json or "{}")
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"直连模型的额外 JSON 参数格式错误：{exc.msg}") from exc
    if not isinstance(extra_body, dict):
        raise RuntimeError("直连模型的额外 JSON 参数必须是 JSON 对象")

    from openai import AsyncOpenAI

    client = AsyncOpenAI(api_key=config.api_key, base_url=config.api_base_url, timeout=config.timeout_seconds)
    try:
        response = await client.chat.completions.create(
            model=config.model_name,
            messages=_to_openai_messages(prompt),
            temperature=temperature if temperature is not None else config.temperature,
            max_tokens=max_tokens if max_tokens is not None else config.max_tokens,
            extra_body=extra_body or None,
        )
    finally:
        await client.close()
    content = response.choices[0].message.content if response.choices else ""
    if not content:
        raise RuntimeError("直连模型没有返回可用正文")
    return {"success": True, "content": content}
