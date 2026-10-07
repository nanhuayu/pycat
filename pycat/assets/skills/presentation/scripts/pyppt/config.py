"""Deterministic canvas and resource budgets; no services or global settings."""
from dataclasses import dataclass
from pathlib import Path

CANVAS_W = 1280.0
CANVAS_H = 720.0
EMU_PER_PX = 9525.0
EMU_PER_PT = 12700.0
MAX_ASSET_BYTES = 16 * 1024 * 1024
@dataclass(frozen=True)
class Settings:
    workspace: Path
