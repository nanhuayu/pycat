from __future__ import annotations


class ThinkingStreamParser:
    """Split <think>...</think> (or <analysis>...</analysis>) from normal content."""

    def __init__(self):
        self._buffer = ""
        self._in_think = False

    def feed(self, text: str) -> tuple[str, str]:
        if not text:
            return "", ""

        self._buffer += text
        out_visible: list[str] = []
        out_thinking: list[str] = []

        while self._buffer:
            if self._in_think:
                end_idx = self._buffer.find("</think>")
                alt_end_idx = self._buffer.find("</analysis>")
                if end_idx == -1 or (alt_end_idx != -1 and alt_end_idx < end_idx):
                    end_idx = alt_end_idx

                if end_idx == -1:
                    out_thinking.append(self._buffer)
                    self._buffer = ""
                    break

                out_thinking.append(self._buffer[:end_idx])
                close_len = len("</think>") if self._buffer.startswith("</think>", end_idx) else len("</analysis>")
                self._buffer = self._buffer[end_idx + close_len :]
                self._in_think = False
                continue

            start_idx = self._buffer.find("<think>")
            alt_start_idx = self._buffer.find("<analysis>")
            if start_idx == -1 or (alt_start_idx != -1 and alt_start_idx < start_idx):
                start_idx = alt_start_idx

            if start_idx == -1:
                out_visible.append(self._buffer)
                self._buffer = ""
                break

            out_visible.append(self._buffer[:start_idx])
            open_len = len("<think>") if self._buffer.startswith("<think>", start_idx) else len("<analysis>")
            self._buffer = self._buffer[start_idx + open_len :]
            self._in_think = True

        return "".join(out_visible), "".join(out_thinking)
