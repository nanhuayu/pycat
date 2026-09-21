"""Run outside the checkout with an installed wheel's interpreter."""

from __future__ import annotations

import asyncio
import importlib.metadata
import importlib.util
import json
import subprocess
import sys
import tempfile
import textwrap
from importlib.resources import files
from pathlib import Path

import httpx
from check_binary import check_export, check_mcp

import pycat
from pycat import ModelProfile, Provider, PyCat, RunRequest, run_sync


def transport():
    async def respond(request):
        payload = {"choices": [{"delta": {"content": "installed answer"}, "finish_reason": "stop"}]}
        return httpx.Response(
            200,
            headers={"content-type": "text/event-stream"},
            text="data: " + json.dumps(payload) + "\n\ndata: [DONE]\n\n",
        )

    return httpx.MockTransport(respond)


def main():
    distribution = importlib.metadata.distribution("pycat")
    assert Path(pycat.__file__).is_relative_to(distribution.locate_file("")), "The checkout masked the wheel"
    assert all(importlib.util.find_spec(name) is None for name in ("PyQt6", "numpy", "onnxruntime", "textual", "fastapi"))
    assert not any(name.startswith(("PyQt6", "numpy", "onnxruntime")) for name in sys.modules)
    for resource in (
        "assets/pycat.svg",
        "assets/styles/base.qss",
        "assets/pycat.ico",
        "assets/ocr/ppocrv6-small/manifest.json",
        "assets/default_models.json",
        "assets/web/index.html",
        "assets/web/app.js",
        "assets/web/surfaces.css",
        "assets/web/vendor/markdown-it.js",
        "assets/web/vendor/markdown-it.LICENSE",
        "assets/web/vendor/manifest.json",
        "tui/workbench.tcss",
    ):
        assert files("pycat").joinpath(resource).is_file(), resource
    assert not files("pycat").joinpath("core/app/default_models.json").is_file()
    assert not any(path.parts[0] in {"core", "gui", "cli", "models", "assets"}
                   for path in distribution.files), "Stale build files leaked into the wheel"
    with tempfile.TemporaryDirectory() as directory:

        async def prepare_and_run():
            async with PyCat(data_dir=directory, transport_factory=transport) as app:
                assert app.models.list() and app.modes.list()
                app.models.save(
                    [Provider(name="test", api_base="https://model.invalid/v1", models=[ModelProfile(model_id="tiny")])]
                )
                first = await app.run(RunRequest(text="installed"))
                assert first.final_message.content == "installed answer"
                return first.conversation.id

        conversation_id = asyncio.run(prepare_and_run())
        result = run_sync(
            RunRequest(text="follow up", conversation_id=conversation_id),
            data_dir=directory,
            transport_factory=transport,
        )
        assert len(result.conversation.messages) == 4
        for name in ("core", "models", "gui", "cli"):
            (Path(directory) / f"{name}.py").write_text(
                "raise AssertionError('Host module was imported')", encoding="utf-8"
            )
        subprocess.run(
            [sys.executable, "-c", "import pycat; from pycat import PyCat, RunRequest"], cwd=directory, check=True
        )
        subprocess.run([sys.executable, "-m", "pycat", "--version"], cwd=directory, check=True)
        subprocess.run([sys.executable, "-m", "pycat", "--help"], cwd=directory, check=True, stdout=subprocess.DEVNULL)
        gui = subprocess.run(
            [sys.executable, "-m", "pycat", "--gui"],
            cwd=directory, text=True, capture_output=True, timeout=30,
        )
        assert gui.returncode == 2 and "pycat[gui]" in gui.stderr, gui
        for command, diagnostic in (("tui", "interactive terminal"), ("serve", "pycat[web]")):
            missing = subprocess.run([sys.executable, "-m", "pycat", command, "--data-dir", directory],
                cwd=directory, stdin=subprocess.DEVNULL, text=True, capture_output=True, timeout=30)
            assert missing.returncode == 2 and diagnostic in missing.stderr, missing

        def worker(code):
            subprocess.run([sys.executable, "-c", textwrap.dedent(code)], cwd=directory, check=True, timeout=60)

        check_mcp(worker, Path(directory))
        check_export(worker)
    print("Installed SDK, CLI, MCP discovery/call, input isolation and resources passed without GUI/OCR dependencies.")


if __name__ == "__main__":
    main()
