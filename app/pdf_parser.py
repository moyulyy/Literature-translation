"""Layout-aware PDF parsing.

The parser turns a PDF into a flat, single-column reading stream made of typed
blocks (title, heading, body, caption, figure, table, equation, reference ...).

Design notes
------------
* PyMuPDF gives us text blocks with coordinates and font information.  Those
  coordinates drive the reading order (two-column papers are re-flowed into a
  single "通栏" stream).
* Figures and tables are cropped from the rendered page instead of being
  translated, so formulas, chemical structures, charts and table cell contents
  stay exactly as published.
* Some publishers (e.g. ACS) place section headings and author lines in the
  text layer with broken coordinates.  We recover those lines from the plain
  text stream and anchor them to the nearest known block.
"""
from __future__ import annotations

import hashlib
import re
from collections import Counter
from dataclasses import asdict, dataclass, field
from pathlib import Path

import pymupdf

COMPOUND_PREFIXES = {
    "first", "second", "third", "fourth", "high", "low", "well", "self", "non",
    "two", "three", "four", "multi", "electro", "photo", "co", "pre", "post",
    "anti", "semi", "quasi", "cross", "long", "short", "large", "small",
    "single", "double", "open", "closed", "one", "nano", "micro", "macro",
    "bio", "hydro", "thermo", "mechano",
}

LIGATURES = {
    "\ufb00": "ff",
    "\ufb01": "fi",
    "\ufb02": "fl",
    "\ufb03": "ffi",
    "\ufb04": "ffl",
    "\ufb05": "st",
    "\ufb06": "st",
}
ORNAMENTS = "■▲●◆▪▫◦□◾◼"
MATH_FONT_RE = re.compile(r"^(STIX|CMMI|CMSY|CMEX|MSAM|MSBM|Symbol|Math|ArnoPro)", re.I)
MATH_CHARS = set(
    "=+−-±∓×÷≈≠≤≥<>∑∏∫∂∇√∞→←↔⇒⇔∈∉⊂⊃∪∩ΔδΩΦφθπμ°′″·⋅|⎛⎝⎞⎠⟨⟩"
)
CAPTION_RE = re.compile(
    r"^(Figure|Fig\.|FIGURE|Table|TABLE|Scheme|SCHEME)\s*([0-9]+[A-Za-z]?)\s*[.:]\s+",
    re.I,
)
REF_RE = re.compile(r"^(?:\((\d{1,3})\)|\[(\d{1,3})\])\s")
AFFIL_RE = re.compile(
    r"(Department|University|Institute|Laborator|School of|College|Faculty|Academy|Center for|Centre for)",
    re.I,
)
FOOTNOTE_RE = re.compile(
    r"^(Received|Revised|Published|Accepted|Notes\b|Corresponding Author|E-mail|\*E-?mail)",
    re.I,
)
HEADING_WORDS = {
    "abstract",
    "introduction",
    "results",
    "discussion",
    "conclusion",
    "conclusions",
    "methods",
    "experimental",
    "acknowledgments",
    "acknowledgements",
    "references",
    "bibliography",
    "notes",
    "author information",
    "associated content",
    "supporting information",
}


@dataclass
class Block:
    id: str
    page: int
    kind: str
    text: str = ""
    translation: str = ""
    asset: str | None = None
    level: int = 0
    label: str = ""
    translate: bool = True
    bbox: list[float] = field(default_factory=list)
    column: str = "full"
    size: float = 0.0
    target: str | None = None
    anchor: str | None = None
    after: str | None = None


@dataclass
class _Raw:
    page: int
    bbox: tuple[float, float, float, float]
    text: str
    lines: list[dict]
    size: float
    bold: bool
    math_ratio: float
    symbol_ratio: float
    column: str = "full"
    id: str = ""
    after: str | None = None


# ---------------------------------------------------------------------------
# text helpers
# ---------------------------------------------------------------------------


def _is_cjk(ch: str) -> bool:
    if not ch:
        return False
    code = ord(ch)
    return (
        0x4E00 <= code <= 0x9FFF
        or 0x3400 <= code <= 0x4DBF
        or 0x3000 <= code <= 0x303F
        or 0xFF00 <= code <= 0xFFEF
    )


def _clean(text: str) -> str:
    for src, dst in LIGATURES.items():
        text = text.replace(src, dst)
    text = text.replace("\u00ad", "").replace("\u200b", "").replace("\ufeff", "")
    text = text.replace("\xa0", " ")
    text = text.replace("firstprinciples", "first-principles")
    # PDF text layers often split a compound after a minus/dash ("metal− air")
    text = re.sub(r"([A-Za-z0-9])−\s+([A-Za-z0-9])", r"\1−\2", text)
    text = re.sub(r"([A-Za-z])-\s+(\d)", r"\1-\2", text)
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r"\s+", " ", text)
    return text.strip()


def _strip_ornaments(text: str) -> str:
    text = text.strip()
    while text and text[0] in ORNAMENTS:
        text = text[1:].lstrip()
    return text


def _join_lines(lines: list[str]) -> str:
    out = ""
    for raw in lines:
        piece = raw.strip()
        if not piece:
            continue
        if not out:
            out = piece
            continue
        if out.endswith("-") and piece[:1].islower() and not _is_cjk(piece[0]):
            prefix = re.search(r"([A-Za-z]+)-$", out)
            if prefix and prefix.group(1).lower() in COMPOUND_PREFIXES:
                out += piece
            else:
                out = out[:-1] + piece
        elif _is_cjk(out[-1]) or _is_cjk(piece[0]):
            out += piece
        else:
            out += " " + piece
    return _clean(out)


def _line_text(line: dict) -> str:
    # PyMuPDF keeps the original spacing inside spans; inserting extra spaces
    # here breaks subscripts (LaMnO3 -> LaMnO 3) and ligatures (first -> fi rst).
    return "".join(span.get("text", "") for span in line.get("spans", []))


def _norm(text: str) -> str:
    return re.sub(r"\s+", " ", text).strip().lower()


def _match_key(text: str) -> str:
    """Comparison key that tolerates subscript spacing differences."""
    return re.sub(r"\s+", "", _strip_ornaments(text)).lower()


def _ends_sentence(text: str) -> bool:
    t = text.rstrip()
    return bool(t) and t[-1] in ".!?:;。”’\"'"


def _overlap_x(a: tuple, b: tuple) -> float:
    return max(0.0, min(a[2], b[2]) - max(a[0], b[0]))


def _area(rect: tuple) -> float:
    return max(0.0, rect[2] - rect[0]) * max(0.0, rect[3] - rect[1])


def _intersection(a: tuple, b: tuple) -> float:
    w = max(0.0, min(a[2], b[2]) - max(a[0], b[0]))
    h = max(0.0, min(a[3], b[3]) - max(a[1], b[1]))
    return w * h


def _gap_y(a: tuple, b: tuple) -> float:
    if b[1] >= a[3]:
        return b[1] - a[3]
    if a[1] >= b[3]:
        return a[1] - b[3]
    return 0.0


# ---------------------------------------------------------------------------
# page extraction
# ---------------------------------------------------------------------------


def _read_page(page: pymupdf.Page, page_no: int) -> dict:
    raw_blocks: list[_Raw] = []
    images: list[tuple[float, float, float, float]] = []
    drawings: list[tuple[float, float, float, float]] = []
    tables: list[tuple[float, float, float, float]] = []

    width = page.rect.width
    for item in page.get_text("dict").get("blocks", []):
        bbox = tuple(float(v) for v in item["bbox"])
        if item.get("type") == 1:
            if (bbox[2] - bbox[0]) > 40 and (bbox[3] - bbox[1]) > 30:
                images.append(bbox)
            continue
        lines = []
        for line in item.get("lines", []):
            text = _line_text(line)
            if not text.strip():
                continue
            sizes = [s.get("size", 10.0) for s in line.get("spans", []) for _ in s.get("text", "")]
            fonts = [s.get("font", "") for s in line.get("spans", []) for _ in s.get("text", "")]
            chars = sum(len(s.get("text", "")) for s in line.get("spans", []))
            size = (sum(sizes) / len(sizes)) if sizes else 10.0
            bold = any((s.get("flags", 0) & 16) or "bold" in s.get("font", "").lower() for s in line.get("spans", []))
            lines.append(
                {
                    "text": text,
                    "bbox": tuple(float(v) for v in line["bbox"]),
                    "size": size,
                    "bold": bold,
                    "font": fonts[0] if fonts else "",
                    "chars": chars,
                    "spans": [
                        {
                            "text": s.get("text", ""),
                            "font": s.get("font", ""),
                            "size": s.get("size", 10.0),
                            "flags": s.get("flags", 0),
                        }
                        for s in line.get("spans", [])
                    ],
                }
            )
        if not lines:
            continue
        text = _join_lines([ln["text"] for ln in lines])
        if not text:
            continue
        total_chars = sum(ln["chars"] for ln in lines) or 1
        size = sum(ln["size"] * ln["chars"] for ln in lines) / total_chars
        bold = any(ln["bold"] for ln in lines)
        math_chars = 0
        symbols = 0
        for ln in lines:
            for ch in ln["text"]:
                if ch in MATH_CHARS:
                    symbols += 1
        for line in item.get("lines", []):
            for span in line.get("spans", []):
                if MATH_FONT_RE.match(span.get("font", "")):
                    math_chars += len(span.get("text", ""))
        bbox = (
            min(ln["bbox"][0] for ln in lines),
            min(ln["bbox"][1] for ln in lines),
            max(ln["bbox"][2] for ln in lines),
            max(ln["bbox"][3] for ln in lines),
        )
        raw_blocks.append(
            _Raw(
                page=page_no,
                bbox=bbox,
                text=text,
                lines=lines,
                size=size,
                bold=bold,
                math_ratio=math_chars / total_chars,
                symbol_ratio=symbols / max(1, len(text)),
                id=f"p{page_no + 1}-t{len(raw_blocks) + 1}",
            )
        )

    try:
        finder = page.find_tables()
        for table in finder.tables:
            bbox = tuple(float(v) for v in table.bbox)
            if _area(bbox) > 2000:
                tables.append(bbox)
    except Exception:
        pass

    for drawing in page.get_drawings():
        rect = tuple(float(v) for v in drawing["rect"])
        if _area(rect) >= 20:
            drawings.append(rect)

    plain_lines = [_clean(line) for line in page.get_text("text").splitlines()]
    plain_lines = [line for line in plain_lines if line]

    return {
        "page": page_no,
        "width": width,
        "height": page.rect.height,
        "blocks": raw_blocks,
        "images": images,
        "drawings": drawings,
        "tables": tables,
        "plain_lines": plain_lines,
    }


def _body_size(pages: list[dict]) -> float:
    weighted: list[tuple[float, int]] = []
    for page in pages:
        for block in page["blocks"]:
            weighted.append((block.size, max(1, len(block.text))))
    if not weighted:
        return 10.0
    weighted.sort()
    total = sum(w for _, w in weighted)
    acc = 0
    for size, weight in weighted:
        acc += weight
        if acc >= total / 2:
            return size
    return 10.0


def _repeated_margins(pages: list[dict]) -> set[str]:
    counter: Counter[str] = Counter()
    for page in pages:
        seen = set()
        for block in page["blocks"]:
            if block.bbox[1] < 70 or block.bbox[1] > page["height"] - 60:
                key = _norm(block.text)
                if 3 < len(key) < 120:
                    seen.add(key)
        counter.update(seen)
    return {text for text, count in counter.items() if count >= max(2, len(pages) // 2)}


def _column_of(bbox: tuple, content_left: float, content_right: float) -> str:
    width = bbox[2] - bbox[0]
    content_width = max(1.0, content_right - content_left)
    if width >= content_width * 0.62:
        return "full"
    center = (bbox[0] + bbox[2]) / 2
    return "left" if center < (content_left + content_right) / 2 else "right"


def _split_title_block(blocks: list[_Raw], body_size: float) -> list[_Raw]:
    """A publisher quirk puts the author line inside the title block.  Split
    lines that are clearly smaller than the title into their own block."""
    candidates = [b for b in blocks if b.size >= body_size * 1.35]
    if not candidates:
        return blocks
    title = max(candidates, key=lambda b: (b.size, len(b.text)))
    if len(title.lines) < 2:
        return blocks
    cut = next(
        (i for i, line in enumerate(title.lines) if line["size"] < body_size * 1.4),
        None,
    )
    if cut is None or cut == 0:
        return blocks
    head, tail = title.lines[:cut], title.lines[cut:]
    title.lines = head
    title.text = _join_lines([line["text"] for line in head])
    title.bbox = (
        min(line["bbox"][0] for line in head),
        min(line["bbox"][1] for line in head),
        max(line["bbox"][2] for line in head),
        max(line["bbox"][3] for line in head),
    )
    title.size = sum(line["size"] * line["chars"] for line in head) / max(1, sum(line["chars"] for line in head))
    extra = _Raw(
        page=title.page,
        bbox=(
            min(line["bbox"][0] for line in tail),
            min(line["bbox"][1] for line in tail),
            max(line["bbox"][2] for line in tail),
            max(line["bbox"][3] for line in tail),
        ),
        text=_join_lines([line["text"] for line in tail]),
        lines=tail,
        size=sum(line["size"] * line["chars"] for line in tail) / max(1, sum(line["chars"] for line in tail)),
        bold=any(line["bold"] for line in tail),
        math_ratio=0.0,
        symbol_ratio=0.0,
        id=f"{title.id}a",
    )
    result = []
    for block in blocks:
        result.append(block)
        if block is title:
            result.append(extra)
    return result


# ---------------------------------------------------------------------------
# classification
# ---------------------------------------------------------------------------


def _is_caption(text: str) -> re.Match | None:
    return CAPTION_RE.match(text.strip())


def _is_reference(block: _Raw, body_size: float) -> bool:
    return bool(REF_RE.match(block.text.strip())) and block.size <= body_size * 0.97


def _is_footnote(block: _Raw, page_height: float, body_size: float) -> bool:
    text = block.text.strip()
    if FOOTNOTE_RE.match(text):
        return True
    if re.match(r"^[a-z]{1,2}\s+[A-Za-z]", text) and block.size < body_size * 0.93 and len(text) < 400:
        return True
    if block.size < body_size * 0.85 and block.bbox[1] > page_height * 0.82:
        return True
    return False


def _is_heading(block: _Raw, body_size: float) -> bool:
    text = _strip_ornaments(block.text)
    if not text or len(text) > 110 or len(text.split()) > 14:
        return False
    if _is_caption(text) or FOOTNOTE_RE.match(text):
        return False
    if text.endswith((",", ";")) or text.endswith("."):
        return False
    if text.lower().strip(":") in HEADING_WORDS:
        return True
    letters = [ch for ch in text if ch.isalpha()]
    if letters and sum(ch.isupper() for ch in letters) / len(letters) > 0.8 and len(text) <= 70:
        return True
    if re.match(r"^\d{1,2}(\.\d{1,2})*\.?\s+[A-Z]", text):
        return True
    if len(text) <= 70 and block.size >= body_size * 1.18:
        return True
    if block.bold and len(text) <= 70 and not text.endswith("."):
        return True
    return False


def _is_equation(block: _Raw, body_size: float) -> bool:
    text = block.text.strip()
    if not text or len(text) > 600 or _is_caption(text):
        return False
    if re.match(r"^[\(\[]?\d{1,3}[\)\]]?\s*$", text):
        return False
    numbered = bool(re.search(r"\(\d{1,2}[a-z]?\)\s*$", text))
    if block.math_ratio >= 0.34:
        return True
    if block.symbol_ratio >= 0.12 and (numbered or len(text) < 180):
        return True
    if numbered and len(text) < 90 and block.symbol_ratio >= 0.04:
        return True
    return False


def _classify(
    block: _Raw,
    body_size: float,
    page_height: float,
    page_no: int,
    title_id: str | None,
    in_references: bool,
) -> tuple[str, bool, int]:
    text = _strip_ornaments(block.text)
    low = text.lower()

    if title_id and block.id == title_id:
        return "title", True, 0
    match = _is_caption(text)
    if match:
        label = f"{match.group(1).title()} {match.group(2)}" if match.group(2) else match.group(0).strip()
        kind = "table_caption" if low.startswith("table") else "figure_caption"
        return kind, True, 0
    if low.startswith("abstract"):
        return "abstract", True, 0
    if _is_reference(block, body_size):
        return "reference", False, 0
    if _is_footnote(block, page_height, body_size):
        return "footnote", False, 0
    if _is_equation(block, body_size):
        return "equation", False, 0
    if page_no == 0 and AFFIL_RE.search(text) and len(text) < 300 and block.bbox[1] < page_height * 0.42:
        return "affiliation", False, 0
    if page_no == 0 and block.bbox[1] < page_height * 0.42 and _looks_like_author_line(text):
        return "authors", False, 0
    if _is_heading(block, body_size):
        return "heading", True, 1
    return "body", True, 0


# ---------------------------------------------------------------------------
# figures / tables / equations
# ---------------------------------------------------------------------------


def _cluster_drawings(rects: list[tuple], gap: float = 6.0) -> list[tuple]:
    clusters: list[list[float]] = []
    for rect in sorted(rects, key=lambda r: (r[1], r[0])):
        placed = False
        for cluster in clusters:
            expanded = (
                cluster[0] - gap,
                cluster[1] - gap,
                cluster[2] + gap,
                cluster[3] + gap,
            )
            if _intersection(expanded, rect) > 0:
                cluster[0] = min(cluster[0], rect[0])
                cluster[1] = min(cluster[1], rect[1])
                cluster[2] = max(cluster[2], rect[2])
                cluster[3] = max(cluster[3], rect[3])
                placed = True
                break
        if not placed:
            clusters.append(list(rect))
    return [tuple(c) for c in clusters]


def _text_coverage(rect: tuple, blocks: list[_Raw]) -> float:
    area = _area(rect)
    if area <= 0:
        return 1.0
    covered = sum(_intersection(rect, b.bbox) for b in blocks)
    return min(1.0, covered / area)


def _crop(page: pymupdf.Page, rect: tuple, path: Path, zoom: float = 2.5) -> str:
    clip = pymupdf.Rect(
        max(0, rect[0] - 1.5),
        max(0, rect[1] - 1.5),
        min(page.rect.width, rect[2] + 1.5),
        min(page.rect.height, rect[3] + 1.5),
    )
    pixmap = page.get_pixmap(clip=clip, matrix=pymupdf.Matrix(zoom, zoom), alpha=False)
    pixmap.save(str(path))
    return path.name


# ---------------------------------------------------------------------------
# main entry point
# ---------------------------------------------------------------------------


def parse_pdf(pdf_path: Path, assets_dir: Path, job_id: str, progress=None) -> dict:
    doc = pymupdf.open(str(pdf_path))
    assets_dir.mkdir(parents=True, exist_ok=True)
    pages = [_read_page(doc[i], i) for i in range(doc.page_count)]
    body_size = _body_size(pages)
    repeated = _repeated_margins(pages)
    content_left = min((b.bbox[0] for p in pages for b in p["blocks"]), default=40.0)
    content_right = max((b.bbox[2] for p in pages for b in p["blocks"]), default=580.0)

    # --- first pass: raw blocks, margin filtering -------------------------
    per_page: dict[int, list[_Raw]] = {}
    fig_assets: dict[str, str] = {}
    table_assets: dict[str, str] = {}
    figure_rects: dict[int, list[tuple]] = {}

    for page in pages:
        page_no = page["page"]
        page_obj = doc[page_no]
        width, height = page["width"], page["height"]
        kept: list[_Raw] = []
        for block in page["blocks"]:
            text = block.text.strip()
            if not text:
                continue
            if block.bbox[1] < 62 and block.size < body_size * 1.02:
                continue
            if block.bbox[1] > height - 52 and block.size < 9.2:
                continue
            if _norm(text) in repeated and (block.bbox[1] < 90 or block.bbox[1] > height - 90):
                continue
            if re.fullmatch(r"\d{1,4}", text):
                continue
            if block.bbox[1] < 95 and block.size < 9 and (
                "pubs.acs.org" in text.lower() or text.lower() == "article"
            ):
                continue
            if page_no == 0 and block.bbox[1] > height - 60:
                # keep the journal line as metadata only, drop from flow
                continue
            if any(
                _intersection(block.bbox, table) / max(1.0, _area(block.bbox)) > 0.45
                for table in page["tables"]
            ):
                continue
            block.column = _column_of(block.bbox, content_left, content_right)
            kept.append(block)
        per_page[page_no] = kept

        # tables: crop whole table regions
        for index, rect in enumerate(page["tables"], start=1):
            tid = f"p{page_no + 1}-tab{index}"
            name = _crop(page_obj, rect, assets_dir / f"{tid}.png", zoom=3.8)
            table_assets[tid] = name

        # image figures
        figure_rects[page_no] = []
        for index, rect in enumerate(page["images"], start=1):
            if any(_intersection(rect, t) / max(1, _area(rect)) > 0.4 for t in page["tables"]):
                continue
            fid = f"p{page_no + 1}-fig{index}"
            name = _crop(page_obj, rect, assets_dir / f"{fid}.png", zoom=4.2)
            fig_assets[fid] = name
            figure_rects[page_no].append(rect)

        if progress:
            progress(page_no + 1, len(pages), f"解析第 {page_no + 1}/{len(pages)} 页")

    # --- classify text blocks --------------------------------------------
    if per_page.get(0):
        per_page[0] = _split_title_block(per_page[0], body_size)
    _split_inline_headings(per_page)
    all_blocks: list[Block] = []
    title_block: Block | None = None
    title_raw_id: str | None = None
    if per_page.get(0):
        candidates = [b for b in per_page[0] if b.size >= body_size * 1.35 and not _is_caption(b.text)]
        if candidates:
            title_raw = max(candidates, key=lambda b: (b.size, len(b.text)))
            title_raw_id = title_raw.id

    for page_no, blocks in per_page.items():
        page_height = pages[page_no]["height"]
        in_references = False
        for index, raw in enumerate(blocks):
            raw.column = _column_of(raw.bbox, content_left, content_right)
            kind, translate, level = _classify(
                raw, body_size, page_height, page_no, title_raw_id, in_references
            )
            if kind == "heading" and _strip_ornaments(raw.text).lower().strip(":") in {
                "references",
                "bibliography",
            }:
                in_references = True
            text = _strip_ornaments(raw.text) if kind == "heading" else raw.text
            if kind == "abstract":
                text = re.sub(r"^abstract\s*[:.]?\s*", "", text, flags=re.I)
            block = Block(
                id=raw.id,
                page=page_no,
                kind=kind,
                text=_clean(text),
                translate=translate,
                level=level,
                bbox=[round(v, 2) for v in raw.bbox],
                column=raw.column,
                size=round(raw.size, 2),
                after=raw.after,
            )
            if kind == "title" and title_block is None:
                title_block = block
            if kind in ("authors", "affiliation") and page_no == 0:
                block.column = "full"
            all_blocks.append(block)

    # detect run-in headings (bold lead-in inside a paragraph) by comparing
    # the first line's font with the rest of the block -- handled at rendering
    # time via ** markers, see below.

    # --- orphan lines: recover headings/authors lost from the block layer --
    all_blocks = _recover_orphans(pages, per_page, all_blocks, body_size)

    # --- figures, tables, equations as elements --------------------------
    elements: dict[int, list[Block]] = {p: [] for p in per_page}
    for block in all_blocks:
        elements.setdefault(block.page, []).append(block)

    for page_no, page in enumerate(pages):
        page_obj = doc[page_no]
        table_map = {f"p{page_no + 1}-tab{i}": rect for i, rect in enumerate(page["tables"], start=1)}
        for tid, rect in table_map.items():
            block = Block(
                id=tid,
                page=page_no,
                kind="table",
                asset=table_assets[tid],
                translate=False,
                bbox=[round(v, 2) for v in rect],
                column=_column_of(rect, content_left, content_right),
            )
            elements[page_no].append(block)
        for index, rect in enumerate(page["images"], start=1):
            fid = f"p{page_no + 1}-fig{index}"
            if fid not in fig_assets:
                continue
            elements[page_no].append(
                Block(
                    id=fid,
                    page=page_no,
                    kind="figure",
                    asset=fig_assets[fid],
                    translate=False,
                    bbox=[round(v, 2) for v in rect],
                    column=_column_of(rect, content_left, content_right),
                )
            )

        # vector figures: only accept a drawing cluster when a Figure caption
        # sits right below it (safe against rules, logos, table borders).
        figure_captions = [
            b
            for b in elements[page_no]
            if b.kind == "figure_caption" and b.bbox
        ]
        clusters = _cluster_drawings(page["drawings"])
        for cluster in clusters:
            if _area(cluster) < 9000:
                continue
            if any(_intersection(cluster, t) / max(1, _area(cluster)) > 0.25 for t in page["tables"]):
                continue
            if any(_intersection(cluster, r) / max(1, _area(cluster)) > 0.5 for r in figure_rects.get(page_no, [])):
                continue
            caption = None
            for cap in figure_captions:
                if _overlap_x(cluster, cap.bbox) / max(1, min(cluster[2] - cluster[0], cap.bbox[2] - cap.bbox[0])) < 0.25:
                    continue
                gap = cap.bbox[1] - cluster[3]
                if -2 <= gap <= 180:
                    caption = cap
                    break
            if not caption:
                continue
            if _text_coverage(cluster, [b for b in per_page[page_no]]) > 0.35:
                continue
            fid = f"p{page_no + 1}-vec{len(elements[page_no])}"
            name = _crop(page_obj, cluster, assets_dir / f"{fid}.png", zoom=4.2)
            elements[page_no].append(
                Block(
                    id=fid,
                    page=page_no,
                    kind="figure",
                    asset=name,
                    translate=False,
                    bbox=[round(v, 2) for v in cluster],
                    column=_column_of(cluster, content_left, content_right),
                )
            )

    # --- merge equation fragments and crop them -------------------------
    for page_no, page in enumerate(pages):
        eqs = [b for b in elements[page_no] if b.kind == "equation" and b.bbox]
        merged: list[Block] = []
        used: set[str] = set()
        page_obj = doc[page_no]
        for eq in sorted(eqs, key=lambda b: (b.bbox[1], b.bbox[0])):
            if eq.id in used:
                continue
            group = [eq]
            used.add(eq.id)
            changed = True
            while changed:
                changed = False
                gx0 = min(b.bbox[0] for b in group)
                gy0 = min(b.bbox[1] for b in group)
                gx1 = max(b.bbox[2] for b in group)
                gy1 = max(b.bbox[3] for b in group)
                for other in eqs:
                    if other.id in used:
                        continue
                    if _gap_y((gx0, gy0, gx1, gy1), other.bbox) <= 6 and _overlap_x(
                        (gx0, gy0, gx1, gy1), other.bbox
                    ) > 2:
                        group.append(other)
                        used.add(other.id)
                        changed = True
            xs0 = min(b.bbox[0] for b in group)
            ys0 = min(b.bbox[1] for b in group)
            xs1 = max(b.bbox[2] for b in group)
            ys1 = max(b.bbox[3] for b in group)
            rect = (xs0, ys0, xs1, ys1)
            eid = f"p{page_no + 1}-eq{len(merged) + 1}"
            name = _crop(page_obj, rect, assets_dir / f"{eid}.png", zoom=4.5)
            merged.append(
                Block(
                    id=eid,
                    page=page_no,
                    kind="equation",
                    text=_clean(" ".join(b.text for b in group)),
                    asset=name,
                    translate=False,
                    bbox=[round(v, 2) for v in rect],
                    column=_column_of(rect, content_left, content_right),
                )
            )
            for b in group:
                if b in elements[page_no]:
                    elements[page_no].remove(b)
        elements[page_no].extend(merged)

    # --- link captions to figures/tables ---------------------------------
    for page_no in elements:
        captions = [b for b in elements[page_no] if b.kind in ("figure_caption", "table_caption")]
        targets = [b for b in elements[page_no] if b.kind in ("figure", "table")]
        for caption in captions:
            best: Block | None = None
            best_score = 1e9
            for target in targets:
                if not caption.bbox or not target.bbox:
                    continue
                overlap = _overlap_x(caption.bbox, target.bbox)
                if overlap / max(1, min(caption.bbox[2] - caption.bbox[0], target.bbox[2] - target.bbox[0])) < 0.25:
                    continue
                gap = caption.bbox[1] - target.bbox[3]
                above = target.bbox[1] - caption.bbox[3]
                if caption.kind == "table_caption":
                    score = gap if -2 <= gap <= 90 else (above + 4 if -2 <= above <= 30 else None)
                else:
                    score = gap if -2 <= gap <= 90 else (above + 4 if -2 <= above <= 20 else None)
                if score is None:
                    continue
                if score < best_score:
                    best_score = score
                    best = target
            if best is not None:
                caption.target = best.id
                if not best.label:
                    best.label = caption.text.split(".")[0][:24]

    # --- reading order ----------------------------------------------------
    ordered: list[Block] = []
    for page_no in range(len(pages)):
        ordered.extend(_order_page(elements.get(page_no, []), content_left, content_right))
    ordered = _merge_continuations(ordered)

    # --- drop duplicate title text from body flow ------------------------
    if title_block:
        title_block.text = _clean(title_block.text)

    # --- table of contents ------------------------------------------------
    toc = []
    for block in ordered:
        if block.kind == "heading":
            block.anchor = f"sec-{block.id}"
            toc.append({"id": block.anchor, "text": block.text, "level": block.level})

    title_translation = title_block.translation if title_block else ""
    authors = [b.text for b in ordered if b.kind in ("authors", "affiliation")]
    document = {
        "job_id": job_id,
        "filename": pdf_path.name,
        "page_count": len(pages),
        "body_size": round(body_size, 2),
        "title": title_block.text if title_block else (pdf_path.stem),
        "title_translation": title_translation,
        "authors": authors,
        "blocks": [asdict(b) for b in ordered],
        "toc": toc,
    }
    if progress:
        progress(len(pages), len(pages), "版面解析完成")
    return document


def _recover_orphans(
    pages: list[dict],
    per_page: dict[int, list[_Raw]],
    all_blocks: list[Block],
    body_size: float,
) -> list[Block]:
    """Recover headings/author lines that exist in the text layer but are not
    returned as positioned blocks (a known ACS/Arbortext quirk)."""
    recovered: dict[str | None, list[Block]] = {}
    orphan_heading_keys: set[str] = set()
    text_updates: dict[str, str] = {}
    for page_no, page in enumerate(pages):
        blocks = per_page.get(page_no, [])
        if not blocks:
            continue
        line_to_block: dict[str, str] = {}
        for raw in blocks:
            for line in raw.lines:
                line_to_block.setdefault(_match_key(line["text"]), raw.id)
        # include table-cell lines in the "known" set so they are never
        # mistaken for missing headings; they simply don't update the anchor.
        known = set(line_to_block)
        for raw in page["blocks"]:
            for line in raw.lines:
                known.add(_match_key(line["text"]))

        def accept(pending: list[str], prev_id: str | None) -> None:
            block = _make_orphan(page_no, pending, prev_id, body_size)
            if block is None or _is_duplicate(block.text, blocks):
                return
            recovered.setdefault(prev_id, []).append(block)
            if block.kind == "heading":
                for line in pending:
                    key = _match_key(line)
                    if len(key) >= 4:
                        orphan_heading_keys.add(key)

        prev_id: str | None = None
        pending: list[str] = []
        for line in page["plain_lines"]:
            key = _match_key(line)
            if key in known:
                if pending:
                    accept(pending, prev_id)
                    pending = []
                if key in line_to_block:
                    prev_id = line_to_block[key]
            else:
                pending.append(line)
        if pending:
            accept(pending, prev_id)

        # positionless headings also leak into the tail of a neighbouring
        # text block; remove them there so the body text stays clean.
        if orphan_heading_keys:
            for raw in blocks:
                kept = [line for line in raw.lines if _match_key(line["text"]) not in orphan_heading_keys]
                if len(kept) != len(raw.lines) and kept:
                    raw.lines = kept
                    raw.text = _join_lines([line["text"] for line in kept])
                    raw.size = sum(line["size"] * line["chars"] for line in kept) / max(
                        1, sum(line["chars"] for line in kept)
                    )
                    raw.bbox = (
                        min(line["bbox"][0] for line in kept),
                        min(line["bbox"][1] for line in kept),
                        max(line["bbox"][2] for line in kept),
                        max(line["bbox"][3] for line in kept),
                    )
                    text_updates[raw.id] = raw.text

    if not recovered:
        return all_blocks

    for block in all_blocks:
        if block.id in text_updates and block.kind not in ("title", "authors", "affiliation"):
            block.text = text_updates[block.id]

    result: list[Block] = []
    for block in all_blocks:
        result.append(block)
        result.extend(recovered.get(block.id, []))
    for extra in recovered.get(None, []):
        result.insert(0, extra)
    return result


def _make_orphan(
    page_no: int,
    lines: list[str],
    prev_id: str | None,
    body_size: float,
) -> Block | None:
    text = _clean(" ".join(lines))
    text = _strip_ornaments(text)
    if not text or len(text) < 4 or len(text) > 200:
        return None
    low = _norm(text)
    if low == "article":
        return None
    if re.fullmatch(r"[\d\W]+", text):
        return None
    if any(
        token in low
        for token in ("pubs.acs.org", "dx.doi.org", "american chemical society", "journal of physical")
    ):
        return None
    if text.lower().startswith(("figure ", "table ", "scheme ")) and CAPTION_RE.match(text):
        kind = "table_caption" if text.lower().startswith("table") else "figure_caption"
        translate = True
    elif _looks_like_orphan_heading(text):
        kind = "heading"
        translate = True
    elif _looks_like_author_line(text):
        kind = "authors"
        translate = False
    elif AFFIL_RE.search(text) and len(text) < 300:
        kind = "affiliation"
        translate = False
    else:
        return None
    digest = hashlib.md5(f"{page_no}:{text}".encode("utf-8")).hexdigest()[:8]
    return Block(
        id=f"p{page_no + 1}-orphan-{digest}",
        page=page_no,
        kind=kind,
        text=text,
        translate=translate,
        level=1 if kind == "heading" else 0,
        bbox=[],
        column="full",
        size=round(body_size, 2),
        after=prev_id,
    )


def _is_inline_heading_line(line: dict) -> bool:
    text = line["text"].strip()
    if not text or len(text) > 80:
        return False
    clean = _strip_ornaments(text)
    if len(clean) < 5 or re.search(r"\d", clean):
        return False
    if clean.endswith((".", ",", ";", ":")):
        return False
    if text[:1] in ORNAMENTS:
        return True
    if clean.upper() == clean and len(clean) >= 8:
        return True
    if clean.upper() == clean and clean.lower().strip(":") in HEADING_WORDS:
        return True
    return False


def _split_inline_headings(per_page: dict[int, list[_Raw]]) -> None:
    """Publisher PDFs sometimes append a section heading to the last line of
    the preceding paragraph.  Split those lines into standalone headings."""
    for page_no, blocks in per_page.items():
        result: list[_Raw] = []
        prev_id: str | None = None
        for raw in blocks:
            if len(raw.lines) <= 1:
                result.append(raw)
                prev_id = raw.id
                continue
            headings: list[tuple[int, dict]] = []
            kept: list[dict] = []
            for index, line in enumerate(raw.lines):
                if _is_inline_heading_line(line):
                    headings.append((index, line))
                else:
                    kept.append(line)
            if not headings or not kept:
                result.append(raw)
                prev_id = raw.id
                continue
            raw.lines = kept
            raw.text = _join_lines([line["text"] for line in kept])
            raw.size = sum(line["size"] * line["chars"] for line in kept) / max(
                1, sum(line["chars"] for line in kept)
            )
            raw.bbox = (
                min(line["bbox"][0] for line in kept),
                min(line["bbox"][1] for line in kept),
                max(line["bbox"][2] for line in kept),
                max(line["bbox"][3] for line in kept),
            )
            result.append(raw)
            for number, (index, line) in enumerate(headings, start=1):
                heading = _Raw(
                    page=page_no,
                    bbox=line["bbox"],
                    text=line["text"],
                    lines=[line],
                    size=line["size"],
                    bold=line["bold"],
                    math_ratio=0.0,
                    symbol_ratio=0.0,
                    id=f"{raw.id}-h{number}",
                    after=raw.id if index > 0 else prev_id,
                )
                result.append(heading)
            prev_id = raw.id
        per_page[page_no] = result


def _is_duplicate(text: str, blocks: list[_Raw]) -> bool:
    """An orphan line that is already part of a multi-line block."""
    key = _match_key(text)
    if len(key) < 25:
        return False
    if text.lower().startswith(
        ("introduction", "computational", "results", "discussion", "conclusion", "abstract")
    ):
        return False
    for block in blocks:
        if key in _match_key(block.text):
            return True
    return False


def _looks_like_orphan_heading(text: str) -> bool:
    if len(text) > 90 or re.search(r"\d", text):
        return False
    if any(ch in text for ch in "=+−<>{}[]()|Δ"):
        return False
    letters = [ch for ch in text if ch.isalpha()]
    if len(letters) < 6:
        return False
    if text.upper() == text:
        return True
    if text.lower().strip(":") in HEADING_WORDS:
        return True
    if re.match(r"^[A-Z][A-Za-z\s,&:\-]{2,70}$", text) and len(text.split()) <= 10:
        return True
    return False


def _looks_like_author_line(text: str) -> bool:
    if re.search(r"\d", text) or "." in text or len(text) > 120:
        return False
    if not re.search(r"\b(and|&)\b", text):
        return False
    words = [
        word
        for word in re.findall(r"[A-Za-z][A-Za-z'\-.]*", text)
        if word.lower() not in {"and", "the", "of", "for", "in", "on", "with"}
    ]
    if not 2 <= len(words) <= 12:
        return False
    caps = sum(1 for word in words if word[0].isupper())
    return caps / len(words) >= 0.9


def _starts_continuation(text: str) -> bool:
    text = (text or "").lstrip()
    return bool(text) and len(text) > 8 and (text[0].islower() or text[0].isdigit())


def _merge_continuations(blocks: list[Block]) -> list[Block]:
    """Join paragraphs that were split by a column or page break.  This is what
    turns the two-column PDF flow into natural single-column prose."""
    referenced = {b.after for b in blocks if b.after}
    removed: set[int] = set()
    for index, current in enumerate(blocks):
        if current.kind != "body" or index in removed:
            continue
        if current.id in referenced or not _starts_continuation(current.text):
            continue
        for back in range(index - 1, -1, -1):
            previous = blocks[back]
            if previous.kind in ("heading", "abstract", "title", "reference"):
                break
            if previous.kind != "body" or back in removed:
                continue
            if current.page - previous.page > 1:
                break
            if previous.id in referenced:
                break
            if _ends_sentence(previous.text):
                continue
            previous.text = f"{previous.text.rstrip()} {current.text.lstrip()}"
            removed.add(index)
            break
    return [block for index, block in enumerate(blocks) if index not in removed]


def _order_page(elements: list[Block], content_left: float, content_right: float) -> list[Block]:
    if not elements:
        return []
    anchored = [e for e in elements if e.after]
    positioned = [e for e in elements if e.bbox and not e.after]
    unpositioned = [e for e in elements if not e.bbox and not e.after]
    full = sorted(
        [e for e in positioned if e.column == "full"],
        key=lambda e: (e.bbox[1], e.bbox[0]),
    )
    full_ids = {id(e) for e in full}
    others = [e for e in positioned if id(e) not in full_ids]
    ordered: list[Block] = []
    cursor = 0.0
    for item in full:
        band = [e for e in others if cursor <= (e.bbox[1] + e.bbox[3]) / 2 < item.bbox[1]]
        ordered.extend(_order_band(band, content_left, content_right))
        ordered.append(item)
        cursor = max(cursor, item.bbox[3])
    rest = [e for e in others if (e.bbox[1] + e.bbox[3]) / 2 >= cursor]
    ordered.extend(_order_band(rest, content_left, content_right))

    # recovered headings and inline headings are anchored to the block they
    # follow, regardless of their (sometimes broken) PDF coordinates.
    grouped: dict[str | None, list[Block]] = {}
    for block in anchored:
        grouped.setdefault(block.after, []).append(block)
    for anchor_id, items in grouped.items():
        if anchor_id is None:
            ordered[0:0] = items
            continue
        index = next((i for i, block in enumerate(ordered) if block.id == anchor_id), None)
        if index is None:
            ordered.extend(items)
        else:
            ordered[index + 1 : index + 1] = items
    ordered.extend(unpositioned)
    return ordered


def _order_band(band: list[Block], content_left: float, content_right: float) -> list[Block]:
    if not band:
        return []
    mid = (content_left + content_right) / 2
    left = sorted([e for e in band if (e.bbox[0] + e.bbox[2]) / 2 < mid], key=lambda e: (e.bbox[1], e.bbox[0]))
    right = sorted([e for e in band if (e.bbox[0] + e.bbox[2]) / 2 >= mid], key=lambda e: (e.bbox[1], e.bbox[0]))
    return left + right
