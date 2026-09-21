from __future__ import annotations

import logging
import os

logger = logging.getLogger(__name__)

try:
    import pathspec
except ImportError:
    pathspec = None


def get_file_tree(root_path: str, max_depth: int = 2) -> str:
    """Generate a workspace file tree for prompt context assembly."""
    root_path = os.path.abspath(root_path)
    if not os.path.exists(root_path):
        return ""

    gitignore_path = os.path.join(root_path, ".gitignore")
    ignore_spec = None
    if pathspec and os.path.exists(gitignore_path):
        try:
            with open(gitignore_path, "r", encoding="utf-8") as handle:
                ignore_spec = pathspec.PathSpec.from_lines("gitwildmatch", handle)
        except Exception as exc:
            logger.debug("Failed to load .gitignore for file tree generation: %s", exc)

    default_ignores = {
        ".git",
        "__pycache__",
        "node_modules",
        ".DS_Store",
        ".vscode",
        ".idea",
        "venv",
        "env",
        "dist",
        "build",
        "coverage",
    }

    tree_lines: list[str] = []

    def _is_ignored(path: str) -> bool:
        name = os.path.basename(path)
        if name in default_ignores or name.endswith(".pyc"):
            return True
        if ignore_spec:
            rel_path = os.path.relpath(path, root_path).replace(os.sep, "/")
            if ignore_spec.match_file(rel_path):
                return True
        return False

    def _walk(current_path: str, current_depth: int, prefix: str = "") -> None:
        if current_depth > max_depth:
            return
        try:
            entries = sorted(os.listdir(current_path))
        except OSError:
            return

        visible_entries = [entry for entry in entries if not _is_ignored(os.path.join(current_path, entry))]
        count = len(visible_entries)
        for index, entry in enumerate(visible_entries):
            is_last = index == count - 1
            full_path = os.path.join(current_path, entry)
            connector = "└── " if is_last else "├── "
            tree_lines.append(f"{prefix}{connector}{entry}")
            if os.path.isdir(full_path):
                next_prefix = prefix + ("    " if is_last else "│   ")
                _walk(full_path, current_depth + 1, next_prefix)

    tree_lines.append(os.path.basename(root_path) + "/")
    _walk(root_path, 1)
    return "\n".join(tree_lines)