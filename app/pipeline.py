"""Translation pipeline: PDF -> layout blocks -> DeepSeek translations -> JSON."""
from __future__ import annotations

import asyncio
import hashlib
import json
import logging
from pathlib import Path

from . import config, storage
from .llm import DeepSeekClient
from .pdf_parser import parse_pdf

logger = logging.getLogger(__name__)


class TranslationCache:
    """Content-addressed translation cache so re-runs don't pay twice."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self._data: dict[str, str] = {}
        self._lock = asyncio.Lock()
        self._loaded = False

    async def ensure_loaded(self) -> None:
        if self._loaded:
            return
        async with self._lock:
            if self._loaded:
                return
            if self.path.exists():
                try:
                    raw = await asyncio.to_thread(self.path.read_text, encoding="utf-8")
                    loaded = json.loads(raw)
                    if isinstance(loaded, dict):
                        self._data = {str(k): str(v) for k, v in loaded.items()}
                except (OSError, json.JSONDecodeError):
                    self._data = {}
            self._loaded = True

    @staticmethod
    def make_key(kind: str, text: str) -> str:
        payload = f"{config.DEEPSEEK_MODEL}|{config.PROMPT_VERSION}|{kind}|{text}"
        return hashlib.sha256(payload.encode("utf-8")).hexdigest()

    async def get(self, key: str) -> str | None:
        await self.ensure_loaded()
        return self._data.get(key)

    async def put_many(self, entries: dict[str, str]) -> None:
        if not entries:
            return
        await self.ensure_loaded()
        async with self._lock:
            self._data.update(entries)
            if len(self._data) > 20000:
                self._data = dict(list(self._data.items())[-20000:])
            await asyncio.to_thread(self._save)

    def _save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(self._data, ensure_ascii=False), encoding="utf-8")
        tmp.replace(self.path)


_cache: TranslationCache | None = None
_cache_lock = asyncio.Lock()


async def get_cache() -> TranslationCache:
    global _cache
    if _cache is None:
        async with _cache_lock:
            if _cache is None:
                _cache = TranslationCache(config.CACHE_DIR / "translations.json")
                await _cache.ensure_loaded()
    return _cache


def _make_batches(blocks: list[dict]) -> list[list[dict]]:
    batches: list[list[dict]] = []
    current: list[dict] = []
    size = 0
    for block in blocks:
        length = len(block.get("text") or "")
        if current and (
            size + length > config.BATCH_MAX_CHARS or len(current) >= config.BATCH_MAX_BLOCKS
        ):
            batches.append(current)
            current = []
            size = 0
        current.append(block)
        size += length
    if current:
        batches.append(current)
    return batches


async def translate_document(job_id: str, document: dict, client: DeepSeekClient) -> None:
    cache = await get_cache()
    blocks = document.get("blocks", [])
    translatable = [b for b in blocks if b.get("translate") and (b.get("text") or "").strip()]
    total = len(translatable)
    state = {"done": 0, "failed": 0}
    pending: list[dict] = []
    for block in translatable:
        key = TranslationCache.make_key(block["kind"], block["text"])
        cached = await cache.get(key)
        if cached:
            block["translation"] = cached
            state["done"] += 1
        else:
            block["_cache_key"] = key
            pending.append(block)

    storage.update_job(
        job_id,
        total_blocks=total,
        translated_blocks=state["done"],
        message=f"正在翻译（{state['done']}/{total} 段，部分命中缓存）…" if total else "无需翻译",
    )
    if not pending:
        _finish_metadata(document)
        return

    batches = _make_batches(pending)
    semaphore = asyncio.Semaphore(config.TRANSLATE_CONCURRENCY)
    progress_lock = asyncio.Lock()
    new_entries: dict[str, str] = {}
    failures: list[str] = []

    async def run_batch(batch: list[dict]) -> None:
        async with semaphore:
            payload = [
                {"id": b["id"], "kind": b["kind"], "text": b["text"]}
                for b in batch
            ]
            result: dict[str, str] = {}
            try:
                result = await client.translate(payload)
            except Exception as exc:  # noqa: BLE001 - degrade to per-block retries
                logger.warning("Batch translation failed (%s), retrying per block", exc)
            missing = [b for b in batch if b["id"] not in result]
            for block in missing:
                try:
                    one = await client.translate(
                        [{"id": block["id"], "kind": block["kind"], "text": block["text"]}]
                    )
                    result.update(one)
                except Exception as exc:  # noqa: BLE001
                    failures.append(f"{block['id']}: {exc}")
                    logger.warning("Block %s failed: %s", block["id"], exc)

            for block in batch:
                translation = result.get(block["id"])
                if translation:
                    block["translation"] = translation
                    if block.get("_cache_key"):
                        new_entries[block["_cache_key"]] = translation
                elif not block.get("translation"):
                    block["translation_error"] = "翻译失败"

            async with progress_lock:
                state["done"] += len(batch)
                state["failed"] = len(failures)
                progress = 28 + int(64 * state["done"] / max(1, total))
                storage.update_job(
                    job_id,
                    progress=min(progress, 92),
                    translated_blocks=state["done"],
                    message=f"正在翻译 {state['done']}/{total} 段…",
                )

    await asyncio.gather(*(run_batch(batch) for batch in batches))
    await cache.put_many(new_entries)
    _finish_metadata(document)
    if failures:
        storage.update_job(
            job_id,
            message=f"翻译完成，{len(failures)} 段未成功，可重新上传重试",
        )


def _finish_metadata(document: dict) -> None:
    blocks = document.get("blocks", [])
    by_id = {block["id"]: block for block in blocks}
    title = next((block for block in blocks if block["kind"] == "title"), None)
    document["title_translation"] = (title or {}).get("translation", "")
    for item in document.get("toc", []):
        block_id = item.get("id", "").removeprefix("sec-")
        block = by_id.get(block_id)
        if block:
            item["text_zh"] = block.get("translation") or block.get("text", "")
    for block in blocks:
        block.pop("_cache_key", None)


async def process_job(job_id: str) -> None:
    job = storage.get_job(job_id)
    if job is None:
        return
    try:
        directory = storage.job_dir(job_id)
        pdf_path = directory / "source.pdf"
        assets_dir = directory / "assets"

        storage.update_job(
            job_id,
            status="parsing",
            progress=3,
            message="正在解析 PDF 版面…",
        )

        def on_parse(done: int, total: int, message: str) -> None:
            progress = 3 + int(22 * done / max(1, total))
            storage.update_job(job_id, progress=progress, message=message)

        document = await asyncio.to_thread(
            parse_pdf, pdf_path, assets_dir, job_id, on_parse
        )
        storage.update_job(
            job_id,
            status="translating",
            progress=27,
            pages=document.get("page_count", 0),
            title=document.get("title", ""),
            total_blocks=sum(1 for b in document["blocks"] if b.get("translate")),
            message="版面解析完成，正在调用 DeepSeek…",
        )

        client = DeepSeekClient()
        try:
            await translate_document(job_id, document, client)
        finally:
            await client.close()

        storage.save_document(job_id, document)
        storage.update_job(
            job_id,
            status="done",
            progress=100,
            title=document.get("title_translation") or document.get("title") or job.get("filename", ""),
            message="翻译完成",
        )
    except Exception as exc:  # noqa: BLE001 - surfacing the message to the UI
        logger.exception("Job %s failed", job_id)
        storage.update_job(
            job_id,
            status="error",
            error=str(exc),
            message="处理失败",
        )
