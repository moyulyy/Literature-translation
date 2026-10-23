"""DeepSeek Responses API client used for academic translation."""
from __future__ import annotations

import asyncio
import json
import logging
from typing import Any

import httpx

from . import config

logger = logging.getLogger(__name__)

TRANSLATION_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "translations": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "id": {"type": "string"},
                    "translation": {"type": "string"},
                },
                "required": ["id", "translation"],
                "additionalProperties": False,
            },
        }
    },
    "required": ["translations"],
    "additionalProperties": False,
}

SYSTEM_PROMPT = """你是一名资深的英文学术论文翻译，面向物理、化学与材料科学期刊。
用户会给你一个 JSON 数组，每项包含 id、kind（title/heading/abstract/body/figure_caption/table_caption）和 text。
请把每一项逐条翻译成简体中文，并以 JSON 返回。

翻译要求：
1. 只翻译文本，不解释、不总结、不补充、不删除信息；每条输入必须返回一条同 id 的译文。
2. 化学式（如 LaMnO3、LaBO3、H2O、LaFeO3）、数学符号、变量（U、ΔG、Φ、E、k）、单位、数字和引用编号保持原样，不要翻译或改写。
3. 图表编号与引用原样保留：Figure 2 -> 图 2，Table 1 -> 表 1，Scheme 3 -> 方案 3。
4. 术语准确、符合中文学术写作习惯；中文通顺，不要逐字硬译。
5. 如果输入中含有 Markdown 粗体标记 **...**，输出中也保留该标记。
6. 标题和小节标题要简洁；图注/表注保持说明性语气。
7. 输出只包含 JSON，不要 Markdown 代码块，不要任何额外文字。

返回格式：
{"translations":[{"id":"...","translation":"..."}]}"""


class DeepSeekError(RuntimeError):
    pass


class DeepSeekClient:
    def __init__(
        self,
        api_key: str | None = None,
        base_url: str | None = None,
        model: str | None = None,
    ) -> None:
        self.api_key = (api_key or config.DEEPSEEK_API_KEY).strip()
        self.base_url = (base_url or config.DEEPSEEK_BASE_URL).rstrip("/")
        self.model = model or config.DEEPSEEK_MODEL
        if not self.api_key:
            raise DeepSeekError("未配置 DEEPSEEK_API_KEY，请在 .env 中设置")
        self._client = httpx.AsyncClient(
            base_url=self.base_url,
            headers={
                "Authorization": f"Bearer {self.api_key}",
                "Content-Type": "application/json",
            },
            timeout=httpx.Timeout(180.0, connect=20.0),
        )

    async def close(self) -> None:
        await self._client.aclose()

    async def translate(self, items: list[dict[str, str]]) -> dict[str, str]:
        """Translate a batch of blocks; returns {id: Chinese text}."""
        if not items:
            return {}
        payload = {
            "model": self.model,
            "instructions": SYSTEM_PROMPT,
            "input": json.dumps(
                [
                    {"id": item["id"], "kind": item.get("kind", "body"), "text": item["text"]}
                    for item in items
                ],
                ensure_ascii=False,
            ),
            "reasoning": {"effort": config.DEEPSEEK_EFFORT},
            "text": {
                "format": {
                    "type": "json_schema",
                    "name": "translations",
                    "schema": TRANSLATION_SCHEMA,
                }
            },
            "max_output_tokens": 16000,
        }

        last_error: Exception | None = None
        for attempt in range(3):
            try:
                response = await self._client.post("/responses", json=payload)
                if response.status_code in (429, 500, 502, 503, 504):
                    raise DeepSeekError(f"DeepSeek 服务繁忙（HTTP {response.status_code}）")
                response.raise_for_status()
                body = response.json()
                if body.get("status") == "failed":
                    raise DeepSeekError(str(body.get("error") or "模型返回失败"))
                if body.get("status") == "incomplete":
                    raise DeepSeekError(f"模型输出被截断：{body.get('incomplete_details')}")
                usage = body.get("usage") or {}
                logger.info(
                    "DeepSeek translated %d blocks, tokens in=%s out=%s",
                    len(items),
                    usage.get("input_tokens"),
                    usage.get("output_tokens"),
                )
                return self._parse(body, {item["id"] for item in items})
            except (httpx.TimeoutException, httpx.TransportError, DeepSeekError) as exc:
                last_error = exc
                if attempt < 2:
                    await asyncio.sleep(1.5 * (attempt + 1) ** 2)
            except (json.JSONDecodeError, ValueError, KeyError) as exc:
                last_error = DeepSeekError(f"无法解析模型返回的 JSON：{exc}")
                if attempt < 2:
                    await asyncio.sleep(1.0)

        raise DeepSeekError(str(last_error or "翻译请求失败"))

    @staticmethod
    def _parse(body: dict[str, Any], wanted: set[str]) -> dict[str, str]:
        text_parts: list[str] = []
        for item in body.get("output", []):
            if item.get("type") != "message":
                continue
            for part in item.get("content", []):
                if part.get("type") in ("output_text", "text") and part.get("text"):
                    text_parts.append(part["text"])
        raw = "\n".join(text_parts).strip()
        if raw.startswith("```"):
            raw = re_fence(raw)
        if not raw:
            raise DeepSeekError("模型没有返回文本")
        try:
            data = json.loads(raw)
        except json.JSONDecodeError:
            start, end = raw.find("{"), raw.rfind("}")
            if start == -1 or end <= start:
                raise
            data = json.loads(raw[start : end + 1])
        rows = data.get("translations") if isinstance(data, dict) else None
        if not isinstance(rows, list):
            raise DeepSeekError("模型返回缺少 translations 数组")
        result: dict[str, str] = {}
        for row in rows:
            if not isinstance(row, dict):
                continue
            block_id = str(row.get("id", "")).strip()
            translation = row.get("translation")
            if block_id in wanted and isinstance(translation, str) and translation.strip():
                result[block_id] = translation.strip()
        if not result:
            raise DeepSeekError("模型返回的译文为空")
        return result


def re_fence(text: str) -> str:
    text = text.strip()
    if text.startswith("```"):
        text = text.split("\n", 1)[1] if "\n" in text else text
        if text.rstrip().endswith("```"):
            text = text.rstrip()[:-3]
    return text.strip()
