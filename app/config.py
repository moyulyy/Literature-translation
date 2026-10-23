"""Application configuration, loaded from .env / environment variables."""
from __future__ import annotations

import os
from pathlib import Path

from dotenv import load_dotenv

BASE_DIR = Path(__file__).resolve().parent.parent
load_dotenv(BASE_DIR / ".env")

DATA_DIR = BASE_DIR / "data"
UPLOAD_DIR = DATA_DIR / "uploads"
JOBS_DIR = DATA_DIR / "jobs"
CACHE_DIR = DATA_DIR / "cache"

for _dir in (DATA_DIR, UPLOAD_DIR, JOBS_DIR, CACHE_DIR):
    _dir.mkdir(parents=True, exist_ok=True)

DEEPSEEK_API_KEY = os.getenv("DEEPSEEK_API_KEY", "").strip()
DEEPSEEK_BASE_URL = os.getenv("DEEPSEEK_BASE_URL", "https://api.deepseek.com").rstrip("/")
DEEPSEEK_MODEL = os.getenv("DEEPSEEK_MODEL", "deepseek-flash")
DEEPSEEK_EFFORT = os.getenv("DEEPSEEK_EFFORT", "low")

# Translation batching. deepseek-flash has a 1M context window; we keep batches
# modest so a single failure only costs one small request.
BATCH_MAX_CHARS = int(os.getenv("BATCH_MAX_CHARS", "6000"))
BATCH_MAX_BLOCKS = int(os.getenv("BATCH_MAX_BLOCKS", "12"))
TRANSLATE_CONCURRENCY = int(os.getenv("TRANSLATE_CONCURRENCY", "3"))

MAX_UPLOAD_MB = int(os.getenv("MAX_UPLOAD_MB", "80"))
ALLOWED_EXTENSIONS = {".pdf"}

# Bump when the translation prompt changes so old cache entries are ignored.
PROMPT_VERSION = "1"
