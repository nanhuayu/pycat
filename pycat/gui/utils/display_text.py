"""Display-only formatting; original conversation and trace data stay intact."""
from datetime import datetime


def single_line(value: object) -> str:
    return " ".join(str(value or "").split())


def message_time(value: datetime, *, full: bool = False) -> str:
    return value.strftime("%Y-%m-%d %H:%M:%S" if full else "%m-%d %H:%M")
