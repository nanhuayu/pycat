"""Canonical provider/model reference helpers."""
from __future__ import annotations

import re
from typing import Any, Tuple


def normalize_provider_name(name: str) -> str:
    raw = str(name or "").strip().lower()
    if not raw:
        return ""
    normalized = re.sub(r"[^a-z0-9]+", "-", raw).strip("-")
    return re.sub(r"-+", "-", normalized)


def split_model_ref(value: str) -> Tuple[str, str]:
    text = str(value or "").strip()
    if not text:
        return "", ""
    if "|" not in text:
        return "", text
    provider_name, model_name = text.split("|", 1)
    return normalize_provider_name(provider_name), str(model_name or "").strip()


def build_model_ref(provider_name: str, model_name: str) -> str:
    provider_token = normalize_provider_name(provider_name)
    model_token = str(model_name or "").strip()
    if provider_token and model_token:
        return f"{provider_token}|{model_token}"
    return model_token or provider_token


def provider_matches_name(provider: Any, provider_name: str) -> bool:
    return normalize_provider_name(getattr(provider, "name", "")) == normalize_provider_name(provider_name)
