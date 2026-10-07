"""单位换算：px / pt / EMU / 英寸。

SlideDSL 用 px（1280x720 画布，96dpi）；OOXML 用 EMU；
`edsdk` 风格编辑工具按规范使用 **pt** 与 **0-based** 索引。
"""

from __future__ import annotations

from .config import CANVAS_H, CANVAS_W, EMU_PER_PT, EMU_PER_PX


# python-pptx 需要 int EMU
def px_to_emu(px: float) -> int:
    return int(round(float(px) * EMU_PER_PX))


def emu_to_px(emu: float) -> float:
    return float(emu) / EMU_PER_PX


def pt_to_emu(pt: float) -> int:
    return int(round(float(pt) * EMU_PER_PT))


def emu_to_pt(emu: float) -> float:
    return float(emu) / EMU_PER_PT


def px_to_pt(px: float) -> float:
    return float(px) * 0.75


def pt_to_px(pt: float) -> float:
    return float(pt) / 0.75


def canvas_emu(width: float = CANVAS_W, height: float = CANVAS_H) -> tuple[int, int]:
    return px_to_emu(width), px_to_emu(height)


def px_str(px: float) -> str:
    return f"{px:g}px"
