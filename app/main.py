"""FastAPI application: upload a PDF, translate it, read the bilingual result."""
from __future__ import annotations

import asyncio
import base64
import logging
import re
from contextlib import asynccontextmanager
from pathlib import Path
from urllib.parse import quote

from fastapi import FastAPI, File, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from markupsafe import Markup, escape

from . import config, storage
from .pipeline import process_job

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
logger = logging.getLogger(__name__)

BASE_DIR = Path(__file__).resolve().parent.parent
templates = Jinja2Templates(directory=str(BASE_DIR / "templates"))
TASKS: set[asyncio.Task] = set()


@asynccontextmanager
async def lifespan(_: FastAPI):
    storage.recover_interrupted_jobs()
    yield
    for task in list(TASKS):
        task.cancel()


app = FastAPI(title="Paper 精译", lifespan=lifespan)
app.mount("/static", StaticFiles(directory=str(BASE_DIR / "static")), name="static")


def _rich(text: str | None) -> Markup:
    """Escape model output, then allow **bold** only."""
    safe = str(escape(text or ""))
    safe = re.sub(r"\*\*(.+?)\*\*", r"<strong>\1</strong>", safe)
    return Markup(safe)


templates.env.filters["rich"] = _rich
templates.env.globals["model_name"] = config.DEEPSEEK_MODEL


@app.get("/", response_class=HTMLResponse)
async def index(request: Request) -> HTMLResponse:
    jobs = storage.list_jobs(limit=10)
    return templates.TemplateResponse(
        request,
        "index.html",
        {"jobs": jobs, "max_mb": config.MAX_UPLOAD_MB},
    )


@app.post("/api/translate")
async def upload(file: UploadFile = File(...)) -> JSONResponse:
    if not file.filename:
        raise HTTPException(status_code=400, detail="缺少文件名")
    suffix = Path(file.filename).suffix.lower()
    if suffix not in config.ALLOWED_EXTENSIONS:
        raise HTTPException(status_code=400, detail="只支持 PDF 文件")
    content = await file.read()
    if not content.startswith(b"%PDF"):
        raise HTTPException(status_code=400, detail="文件不是有效的 PDF")
    if len(content) > config.MAX_UPLOAD_MB * 1024 * 1024:
        raise HTTPException(status_code=413, detail=f"文件超过 {config.MAX_UPLOAD_MB} MB")
    job = storage.create_job(Path(file.filename).name, content)
    task = asyncio.create_task(process_job(job["id"]))
    TASKS.add(task)
    task.add_done_callback(TASKS.discard)
    return JSONResponse({"job_id": job["id"]}, status_code=202)


@app.get("/api/jobs/{job_id}")
async def job_status(job_id: str) -> JSONResponse:
    job = storage.get_job(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="任务不存在")
    return JSONResponse(job)


def _zh_label(label: str | None, fallback: str = "图") -> str:
    if not label:
        return fallback
    match = re.match(r"(Figure|Fig\.?|Scheme)\s*([0-9A-Za-z]+)", label, re.I)
    if match:
        return f"图 {match.group(2)}"
    match = re.match(r"Table\s*([0-9A-Za-z]+)", label, re.I)
    if match:
        return f"表 {match.group(1)}"
    return fallback


def _strip_caption_prefix(text: str | None) -> str:
    text = (text or "").strip()
    text = re.sub(r"^(图|表)\s*[0-9A-Za-z]+\s*[.:：]\s*", "", text)
    text = re.sub(
        r"^(Figure|Fig\.?|Table|Scheme)\s*[0-9A-Za-z]+\s*[.:]\s*",
        "",
        text,
        flags=re.I,
    )
    return text.strip()


SUPPRESSED_SECTIONS = {
    "author information",
    "authors information",
    "acknowledgment",
    "acknowledgments",
    "acknowledgement",
    "acknowledgements",
    "references",
    "reference",
    "bibliography",
    "notes",
    "作者信息",
    "致谢",
    "参考文献",
    "参考书目",
}


def _section_key(text: str | None) -> str:
    text = (text or "").strip().lower()
    text = re.sub(r"^\s*\d+(?:\.\d+)*[.)]?\s*", "", text)
    text = re.sub(r"[\s:：.。]+", " ", text)
    return text.strip()


def build_view(document: dict) -> dict:
    blocks = document.get("blocks", [])
    attached: dict[str, dict] = {}
    for block in blocks:
        if block.get("kind") in ("figure_caption", "table_caption") and block.get("target"):
            attached.setdefault(block["target"], block)
    skip = {block["id"] for block in attached.values()}
    skip |= {
        block["id"]
        for block in blocks
        if block.get("kind") in ("title", "authors", "affiliation")
    }

    items: list[dict] = []
    suppress_section = False

    for block in blocks:
        if block["id"] in skip:
            continue
        kind = block.get("kind")

        if kind == "heading":
            keys = {
                _section_key(block.get("text")),
                _section_key(block.get("translation")),
            }
            if keys & SUPPRESSED_SECTIONS:
                suppress_section = True
                continue
            suppress_section = False

        if suppress_section or kind == "reference":
            continue

        if kind == "heading":
            items.append(
                {
                    "type": "heading",
                    "anchor": block.get("anchor") or f"sec-{block['id']}",
                    "text_zh": block.get("translation") or block.get("text", ""),
                    "text_en": block.get("text", ""),
                    "level": block.get("level", 1),
                }
            )
        elif kind == "abstract":
            items.append(
                {
                    "type": "abstract",
                    "zh": block.get("translation") or block.get("text", ""),
                    "en": block.get("text", ""),
                }
            )
        elif kind == "body":
            items.append(
                {
                    "type": "body",
                    "zh": block.get("translation") or block.get("text", ""),
                    "en": block.get("text", ""),
                    "untranslated": not block.get("translation"),
                }
            )
        elif kind in ("figure", "table"):
            caption = attached.get(block["id"])
            fallback = "表" if kind == "table" else "图"
            label = _zh_label(block.get("label"), fallback)
            caption_zh = _strip_caption_prefix((caption or {}).get("translation"))
            caption_en = (caption or {}).get("text") or ""
            items.append(
                {
                    "type": kind,
                    "asset": block.get("asset"),
                    "label": label,
                    "caption_zh": caption_zh,
                    "caption_en": caption_en,
                    "alt": caption_en or ("图形摘要" if not caption else label),
                    "graphical_abstract": block.get("page") == 0 and caption is None,
                }
            )
        elif kind == "equation":
            number = re.search(r"\((\d+[a-z]?)\)\s*$", block.get("text", ""))
            items.append(
                {
                    "type": "equation",
                    "asset": block.get("asset"),
                    "alt": block.get("text", ""),
                    "number": number.group(1) if number else "",
                }
            )
        elif kind in ("figure_caption", "table_caption"):
            label = _zh_label(block.get("label") or block.get("text"), "图")
            items.append(
                {
                    "type": "caption",
                    "label": label,
                    "zh": _strip_caption_prefix(block.get("translation")),
                    "en": block.get("text", ""),
                }
            )
        elif kind == "footnote":
            text = block.get("text", "")
            if re.match(r"^(Received|Revised|Published|Accepted)\b", text, re.I):
                continue
            items.append({"type": "footnote", "text": text})
        elif block.get("text"):
            items.append(
                {
                    "type": "note",
                    "text": block.get("text", ""),
                    "zh": block.get("translation") or "",
                }
            )

    heading_items = [item for item in items if item["type"] == "heading"]
    for index, item in enumerate(heading_items, start=1):
        item["number"] = f"{index:02d}"

    title_block = next((b for b in blocks if b.get("kind") == "title"), None)
    abstract = next((item for item in items if item["type"] == "abstract"), None)
    if abstract:
        items.remove(abstract)

    authors = [b.get("text", "") for b in blocks if b.get("kind") == "authors"]
    affiliations = [b.get("text", "") for b in blocks if b.get("kind") == "affiliation"]
    failed = sum(1 for b in blocks if b.get("translation_error"))

    return {
        "title_zh": document.get("title_translation")
        or (title_block or {}).get("translation")
        or (title_block or {}).get("text", ""),
        "title_en": (title_block or {}).get("text", document.get("title", "")),
        "authors": authors,
        "affiliations": affiliations,
        "abstract": abstract,
        "items": items,
        "failed": failed,
        "pages": document.get("page_count", 0),
    }


_CSS_CACHE: str | None = None


def _css() -> str:
    global _CSS_CACHE
    if _CSS_CACHE is None:
        _CSS_CACHE = (BASE_DIR / "static" / "style.css").read_text(encoding="utf-8")
    return _CSS_CACHE


def _data_uri(path: Path) -> str:
    encoded = base64.b64encode(path.read_bytes()).decode("ascii")
    return f"data:image/png;base64,{encoded}"


def build_download_html(job: dict, document: dict) -> str:
    view = build_view(document)
    for item in view["items"]:
        asset = item.get("asset")
        if not asset:
            continue
        path = storage.job_dir(job["id"]) / "assets" / asset
        if path.is_file():
            item["src"] = _data_uri(path)
    return templates.get_template("download.html").render(
        job=job,
        view=view,
        css=_css(),
        model_name=config.DEEPSEEK_MODEL,
    )


@app.get("/doc/{job_id}", response_class=HTMLResponse)
async def reader(request: Request, job_id: str) -> HTMLResponse:
    job = storage.get_job(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="任务不存在")
    if job.get("status") != "done":
        response = templates.TemplateResponse(
            request,
            "status.html",
            {"job": job, "poll": job.get("status") != "error"},
        )
        response.headers["Cache-Control"] = "no-store"
        return response
    document = storage.load_document(job_id)
    if document is None:
        raise HTTPException(status_code=500, detail="译文数据缺失，请重新上传")
    view = build_view(document)
    response = templates.TemplateResponse(
        request,
        "reader.html",
        {"job": job, "doc": document, "view": view},
    )
    response.headers["Cache-Control"] = "no-store"
    return response


@app.get("/media/{job_id}/{filename}")
async def media(job_id: str, filename: str) -> FileResponse:
    if not re.fullmatch(r"[A-Za-z0-9_.-]+", filename):
        raise HTTPException(status_code=404, detail="资源不存在")
    try:
        path = storage.job_dir(job_id) / "assets" / filename
    except ValueError:
        raise HTTPException(status_code=404, detail="任务不存在")
    if not path.is_file():
        raise HTTPException(status_code=404, detail="资源不存在")
    return FileResponse(
        path,
        media_type="image/png",
        headers={"Cache-Control": "public, max-age=86400"},
    )


@app.get("/source/{job_id}")
async def source(job_id: str) -> FileResponse:
    job = storage.get_job(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="任务不存在")
    try:
        path = storage.job_dir(job_id) / "source.pdf"
    except ValueError:
        raise HTTPException(status_code=404, detail="任务不存在")
    if not path.is_file():
        raise HTTPException(status_code=404, detail="源文件不存在")
    return FileResponse(
        path,
        media_type="application/pdf",
        headers={"Content-Disposition": "inline"},
    )


@app.get("/download/source/{job_id}")
async def download_source(job_id: str) -> FileResponse:
    job = storage.get_job(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="任务不存在")
    try:
        path = storage.job_dir(job_id) / "source.pdf"
    except ValueError:
        raise HTTPException(status_code=404, detail="任务不存在")
    if not path.is_file():
        raise HTTPException(status_code=404, detail="源文件不存在")
    return FileResponse(
        path,
        media_type="application/pdf",
        filename=job.get("filename", "paper.pdf"),
    )


@app.get("/download/translation/{job_id}")
async def download_translation(job_id: str) -> Response:
    job = storage.get_job(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="任务不存在")
    if job.get("status") != "done":
        raise HTTPException(status_code=409, detail="译文尚未生成")
    document = storage.load_document(job_id)
    if document is None:
        raise HTTPException(status_code=500, detail="译文数据缺失")
    html = build_download_html(job, document)
    stem = Path(job.get("filename", "paper.pdf")).stem
    filename = quote(f"{stem}_中文译文.html")
    return Response(
        content=html,
        media_type="text/html; charset=utf-8",
        headers={
            "Content-Disposition": f"attachment; filename*=UTF-8''{filename}",
            "Cache-Control": "no-store",
        },
    )


@app.get("/api/health")
async def health() -> dict:
    return {"status": "ok", "model": config.DEEPSEEK_MODEL}
