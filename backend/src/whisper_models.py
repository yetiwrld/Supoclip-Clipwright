"""Whisper checkpoint catalogue (standard library only).

Shared by the runtime (``src.local_ai``) and by ``scripts/setup_local_ai.py``,
which runs on the host before the backend virtualenv may even exist. Keeping
this module import-free means the setup script can load it directly from disk
without pulling in FastAPI, httpx or torch.

The URLs and their SHA-256 checksums come from ``openai-whisper``'s own
``_MODELS`` table; the checksum is the directory component of each URL.
"""

from __future__ import annotations

import os
from pathlib import Path

WHISPER_MODEL_URLS: dict[str, str] = {
    "tiny.en": "https://openaipublic.azureedge.net/main/whisper/models/d3dd57d32accea0b295c96e26691aa14d8822fac7d9d27d5dc00b4ca2826dd03/tiny.en.pt",
    "tiny": "https://openaipublic.azureedge.net/main/whisper/models/65147644a518d12f04e32d6f3b26facc3f8dd46e5390956a9424a650c0ce22b9/tiny.pt",
    "base.en": "https://openaipublic.azureedge.net/main/whisper/models/25a8566e1d0c1e2231d1c762132cd20e0f96a85d16145c3a00adf5d1ac670ead/base.en.pt",
    "base": "https://openaipublic.azureedge.net/main/whisper/models/ed3a0b6b1c0edf879ad9b11b1af5a0e6ab5db9205f891f668f8b0e6c6326e34e/base.pt",
    "small.en": "https://openaipublic.azureedge.net/main/whisper/models/f953ad0fd29cacd07d5a9eda5624af0f6bcf2258be67c92b79389873d91e0872/small.en.pt",
    "small": "https://openaipublic.azureedge.net/main/whisper/models/9ecf779972d90ba49c06d968637d720dd632c55bbf19d441fb42bf17a411e794/small.pt",
    "medium.en": "https://openaipublic.azureedge.net/main/whisper/models/d7440d1dc186f76616474e0ff0b3b6b879abc9d1a4926b7adfa41db2d497ab4f/medium.en.pt",
    "medium": "https://openaipublic.azureedge.net/main/whisper/models/345ae4da62f9b3d59415adc60127b97c714f32e89e936602e85993674d08dcb1/medium.pt",
    "large-v1": "https://openaipublic.azureedge.net/main/whisper/models/e4b87e7e0bf463eb8e6956e646f1e277e901512310def2c24bf0e11bd3c28e9a/large-v1.pt",
    "large-v2": "https://openaipublic.azureedge.net/main/whisper/models/81f7c96c852ee8fc832187b0132e569d6c3065a3252ed18e56effd0b6a73e524/large-v2.pt",
    "large-v3": "https://openaipublic.azureedge.net/main/whisper/models/e5b1a55b89c1367dacf97e3e19bfd829a01529dbfdeefa8caeb59b3f1b81dadb/large-v3.pt",
    "large-v3-turbo": "https://openaipublic.azureedge.net/main/whisper/models/aff26ae408abcba5fbf8813c21e62b0941638c5f6eebfb145be0c9839262a19a/large-v3-turbo.pt",
}

WHISPER_MODEL_SIZES_MB: dict[str, int] = {
    "tiny.en": 75,
    "tiny": 75,
    "base.en": 142,
    "base": 142,
    "small.en": 466,
    "small": 466,
    "medium.en": 1500,
    "medium": 1500,
    "large-v1": 2900,
    "large-v2": 2900,
    "large-v3": 2900,
    "large-v3-turbo": 1550,
}

WHISPER_ALIASES = {
    "large": "large-v3",
    "turbo": "large-v3-turbo",
}

DEFAULT_WHISPER_MODEL = "small"


def normalize_model_name(name: str | None) -> str:
    normalized = (name or "").strip().lower()
    if not normalized:
        return DEFAULT_WHISPER_MODEL
    return WHISPER_ALIASES.get(normalized, normalized)


def model_url(name: str | None) -> str | None:
    return WHISPER_MODEL_URLS.get(normalize_model_name(name))


def checkpoint_filename(name: str | None) -> str:
    url = model_url(name)
    if url:
        return os.path.basename(url)
    return Path(name or "").name


def expected_sha256(name: str | None) -> str | None:
    url = model_url(name)
    if not url:
        return None
    return url.rstrip("/").split("/")[-2]


def checkpoint_path(name: str | None, directory: str | Path) -> Path:
    return Path(directory) / checkpoint_filename(name)
