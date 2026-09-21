"""Build a selected Windows distribution, verify it, then package its full closure.

Run with the intended build environment's Python. Dependencies are installed
explicitly by the caller; this script never upgrades an environment.
"""
from __future__ import annotations

import argparse
import ast
import hashlib
import importlib.metadata
import json
import os
import shutil
import subprocess
import sys
import xml.etree.ElementTree as ET
import zipfile
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
CACHE = ROOT / "build" / "nuitka-cache"


def version() -> str:
    tree = ast.parse((ROOT / "pycat/core/version.py").read_text(encoding="utf-8"))
    return next(ast.literal_eval(node.value) for node in tree.body if isinstance(node, ast.Assign)
                and any(isinstance(target, ast.Name) and target.id == "__version__" for target in node.targets))


def entry_plan(frontend="all", ocr=True, compiler="auto", jobs=4, analyze=False, *, entry=None) -> dict:
    entry = entry or ("gui" if frontend == "gui" else "cli")
    build_dir = ROOT / "build/nuitka" / f"{frontend}-{'ocr' if ocr else 'no-ocr'}" / entry
    # Validate the only tree Nuitka may clean/replace, including symlink resolution.
    build_dir.resolve().relative_to((ROOT / "build/nuitka").resolve())
    binary = "pycat.exe" if entry == "gui" else "pycat-cli.exe"
    command = [
        sys.executable, "-m", "nuitka", "--standalone",
        f"--output-dir={build_dir}", f"--output-filename={binary}", f"--jobs={jobs}",
        f"--report={build_dir / 'compilation-report.xml'}",
        "--windows-console-mode=" + ("disable" if entry == "gui" else "force"),
        "--product-name=PyCat", "--company-name=PyCat Contributors",
        f"--file-version={version()}", f"--product-version={version()}",
        "--windows-icon-from-ico=pycat/assets/pycat.ico",
        "--include-module=pycat.core.tools.system.python_worker",
        "--include-module=pycat.core.tools.pty_child",
        # ddgs lazily imports its implementation and discovers engines via pkgutil.
        "--include-module=ddgs.ddgs", "--include-package=ddgs.engines",
        "--include-package=textual.drivers", "--include-package=uvicorn",
        # Rich loads Unicode width tables by versioned module names at runtime.
        "--include-package=rich",
        # PyMuPDF's generated Python bindings expand into exceptionally large C
        # units. Its native PDF engine stays bundled; only wrappers use bytecode.
        "--lto=no",
        "--noinclude-dlls=cv2/opencv_videoio_ffmpeg*.dll",
        # The Python CPU provider lives in its pybind extension; the separately
        # distributed C API DLL is unused. Real OCR inference gates this choice.
        "--noinclude-dlls=onnxruntime/capi/onnxruntime.dll",
        "--noinclude-dlls=PyQt6/Qt6/plugins/imageformats/qpdf.dll",
        "--noinclude-dlls=Qt6Pdf.dll",
        "--noinclude-pytest-mode=nofollow", "--noinclude-setuptools-mode=nofollow",
        "--nofollow-import-to=tkinter", "--assume-yes-for-downloads",
        "--include-data-files=LICENSE=LICENSE",
        "--include-data-files=README.md=README.md",
        "--include-data-files=README_en.md=README_en.md",
        "--include-package-data=pycat:assets/default_models.json",
        "--include-package-data=pycat:assets/skills/*",
        "--include-package-data=pycat:assets/extensions/*",
        "--include-package-data=pycat:assets/templates/*",
        "--include-package-data=pycat:assets/web/*",
        "--include-package-data=pycat:assets/pycat.svg",
        "--include-package-data=pycat.tui:*.tcss",
        "--include-package-data=textual",
    ]
    # Keep numeric/image engines native; compile our code while retaining the
    # IO/protocol and binding packages as bundled bytecode. Verified in the final
    # distribution, not substituted with development-environment imports.
    bytecode_packages = ("pymupdf", "mcp", "mcp_types", "pydantic", "anyio", "numpy",
                         "httpx", "httpcore", "httpx2", "httpcore2", "docx", "PIL", "cryptography",
                         "click", "urllib3", "requests", "markdown", "uvicorn", "starlette",
                         "websockets", "jsonschema", "pathspec", "qrcode",
                         "textual", "rich", "pygments", "fastapi")
    command += [f"--noinclude-custom-mode={name}:bytecode" for name in bytecode_packages]
    # These packages query importlib.metadata.version at runtime. Bytecode does
    # not receive Nuitka's compile-time constant substitution for those calls.
    command += [f"--include-distribution-metadata={name}" for name in
                ("httpx2", "httpcore2", "mcp", "Markdown", "textual")]
    if compiler == "msvc":
        command += ["--msvc=latest"]
    elif compiler == "mingw64":
        command += ["--mingw64"]
    if frontend == "cli":
        command += ["--nofollow-import-to=pycat.gui,PyQt6"]
    else:
        command += ["--enable-plugin=pyqt6",
                    "--include-package-data=PyQt6:Qt6/translations/qtbase_zh_CN.qm",
                    "--include-package-data=pycat:assets/*.svg",
                    "--include-package-data=pycat:assets/*.ico",
                    "--include-package-data=pycat:assets/styles/*"]
    if not ocr:
        command += ["--nofollow-import-to=numpy,onnxruntime,cv2,pyclipper,pycat.core.content.ppocrv6"]
    else:
        command += ["--include-package-data=pycat:assets/ocr/*"]
    if frontend == "cli" or not ocr:
        # Intentionally absent features need normal Python import semantics.
        # Nuitka's diagnostic stubs otherwise make find_spec report excluded
        # packages as present and replace ModuleNotFoundError with ImportError.
        command += ["--no-deployment-flag=excluded-module-usage"]
    if analyze:
        command.append("--generate-c-only")
    command.append("main.py" if entry == "gui" else "main_cli.py")
    return {"frontend": frontend, "entry": entry, "ocr": ocr, "binary": binary, "build_dir": str(build_dir),
            "command": command}


def build_plans(frontend="all", ocr=True, compiler="auto", jobs=4, analyze=False) -> list[dict]:
    return [entry_plan(frontend, ocr, compiler, jobs, analyze, entry=entry)
            for entry in (("gui", "cli") if frontend == "all" else (frontend,))]


def merge_distributions(sources: list[Path], destination: Path) -> None:
    """Union complete standalone closures; differing same-path files are a bug."""
    files: dict[Path, Path] = {}
    for source in sources:
        for path in source.rglob("*"):
            if not path.is_file():
                continue
            relative = path.relative_to(source)
            previous = files.get(relative)
            if previous is not None:
                with previous.open("rb") as left, path.open("rb") as right:
                    if hashlib.file_digest(left, "sha256").digest() != hashlib.file_digest(right, "sha256").digest():
                        raise RuntimeError(f"Distribution conflict: {relative}")
            else:
                files[relative] = path
    destination.mkdir(parents=True, exist_ok=False)
    for relative, source in files.items():
        target = destination / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, target)


def inventory(directory: Path) -> dict:
    files = [(path.relative_to(directory).as_posix(), path.stat().st_size)
             for path in directory.rglob("*") if path.is_file()]
    return {"bytes": sum(size for _, size in files), "files": len(files),
            "executables": {name: size for name, size in files if name.endswith(".exe")},
            "largest_files": [{"path": name, "bytes": size} for name, size in sorted(files, key=lambda v: -v[1])[:20]]}


def package_distribution(directory: Path, archive: Path) -> None:
    """The extracted folder stays stable across versions and build variants."""
    with zipfile.ZipFile(archive, "x", compression=zipfile.ZIP_DEFLATED, compresslevel=9) as zipped:
        for path in sorted(directory.rglob("*")):
            if path.is_file():
                zipped.write(path, Path("pycat-windows-x64") / path.relative_to(directory))


def report_summary(build_dir: Path) -> dict:
    report = build_dir / "compilation-report.xml"
    if not report.is_file():
        return {}
    root = ET.parse(report).getroot()
    modules = [node.attrib for node in root.findall("module")]
    return {"modules": len(modules),
            "module_kinds": dict(Counter(row.get("kind", "unknown") for row in modules)),
            "c_files": sum(1 for _ in build_dir.glob("*.build/*.c"))}


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--frontend", choices=("all", "gui", "cli"), default="all")
    parser.add_argument("--without-ocr", action="store_true", help="Omit local OCR libraries AND model resources.")
    parser.add_argument("--compiler", choices=("auto", "msvc", "mingw64"), default="auto")
    parser.add_argument("--jobs", type=int, default=4)
    parser.add_argument("--output-root", type=Path, default=ROOT / "release")
    parser.add_argument("--plan", action="store_true", help="Print the selected commands/resources without building.")
    parser.add_argument("--analyze", action="store_true", help="Generate C and a report without compiling/publishing.")
    args = parser.parse_args(argv)
    if args.jobs < 1:
        parser.error("--jobs must be positive")
    if args.plan:
        print(json.dumps(build_plans(args.frontend, not args.without_ocr, args.compiler, args.jobs, args.analyze), indent=2))
        return 0
    if sys.platform != "win32":
        parser.error("Native Windows releases must be built on Windows.")
    output_root = args.output_root.resolve()
    label = f"pycat-{version()}-windows-x64"
    if args.frontend != "all" or args.without_ocr:
        label += f"-{args.frontend}-{'no-ocr' if args.without_ocr else 'ocr'}"
    archive = output_root / (label + ".zip")
    if archive.exists() and not args.analyze:
        parser.error(f"Release already exists: {archive}. Use --output-root to build another copy.")
    required = ["Nuitka", "httpx", "Pillow", "PyMuPDF", "python-docx", "mcp", "ddgs", "psutil",
                "pywinpty", "textual", "fastapi", "uvicorn"]
    if args.frontend != "cli":
        required += ["PyQt6", "pyte"]
    if not args.without_ocr:
        required += ["numpy", "onnxruntime", "opencv-python-headless", "pyclipper"]
    packages = {}
    for name in required:
        try:
            installed_version = importlib.metadata.version(name)
        except importlib.metadata.PackageNotFoundError:
            parser.error(f"Build dependency missing: {name}. Install the selected project extras and Nuitka first.")
        if name == "mcp" and not (installed_version.startswith("2.") and int(installed_version.split(".")[1]) >= 2):
            parser.error("This application uses MCP SDK >=2.2,<3. Install requirements.txt before building.")
        packages[name] = installed_version
    env = dict(os.environ)
    env["NUITKA_CACHE_DIR"] = str(CACHE)
    env["PYTHONIOENCODING"] = "utf-8"
    env["PYTHONUTF8"] = "1"
    if not args.analyze:
        subprocess.run([sys.executable, str(ROOT / 'scripts/build_public_intro.py'), '--check'],
                       cwd=ROOT, env=env, check=True)
        subprocess.run([sys.executable, str(ROOT / 'scripts/prepare_ripgrep.py')], cwd=ROOT, check=True)
    # Nuitka owns compiler discovery and platform/Conda DLL handling.
    plans = build_plans(args.frontend, not args.without_ocr, args.compiler, args.jobs, args.analyze)
    summaries, directories = {}, []
    for plan in plans:
        build_dir = Path(plan["build_dir"])
        build_dir.mkdir(parents=True, exist_ok=True)
        (build_dir / "build-plan.json").write_text(json.dumps(plan, indent=2), encoding="utf-8")
        subprocess.run(plan["command"], cwd=ROOT, env=env, check=True)
        summaries[plan["entry"]] = report_summary(build_dir)
        if not args.analyze:
            directory = build_dir / ("main.dist" if plan["entry"] == "gui" else "main_cli.dist")
            if not (directory / plan["binary"]).is_file():
                raise RuntimeError("Nuitka did not produce the expected complete distribution.")
            directories.append(directory)
    if args.analyze:
        print(json.dumps(summaries, indent=2))
        return 0
    output_root.mkdir(parents=True, exist_ok=True)
    label += "-" + datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
    final = output_root / label
    # A unique output preserves previous releases, including when verification fails.
    merge_distributions(directories, final)
    subprocess.run([sys.executable, str(ROOT / 'scripts/build_public_intro.py'), '--directory', str(final)],
                   cwd=ROOT, env=env, check=True)
    # This source runs on the remote system's Python, never the frozen host.
    # Nuitka deliberately excludes .py data; ship the single canonical source explicitly.
    helper = Path("pycat/core/hosts/remote_helper.py")
    (final / helper).parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(ROOT / helper, final / helper)
    # Nuitka's package-data rules intentionally omit executables; copy the
    # verified helper and its licenses explicitly into the final distribution.
    shutil.copytree(ROOT / 'pycat/assets/tools/ripgrep/windows-x64',
                    final / 'pycat/assets/tools/ripgrep/windows-x64', dirs_exist_ok=True)
    check = [sys.executable, str(ROOT / "scripts/check_binary.py"), str(final), "--frontend", args.frontend]
    if args.without_ocr:
        check.append("--without-ocr")
    result = subprocess.run(check, cwd=build_dir, env=env, text=True, encoding="utf-8", errors="replace",
                            capture_output=True, check=False)
    (build_dir / "binary-check.log").write_text(result.stdout + result.stderr, encoding="utf-8")
    if result.returncode:
        raise RuntimeError(f"Binary verification failed; no ZIP published:\n{result.stdout}\n{result.stderr}")
    manifest = {"plans": plans, "frontend": args.frontend, "ocr": not args.without_ocr, "version": version(), "python": sys.version, "compiler": args.compiler,
                "packages": packages,
                "compilation": summaries, "checks": json.loads(result.stdout), "distribution": inventory(final)}
    (final / "build-report.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    # Compression failure leaves diagnostics beside this unique build, never a
    # partially written public ZIP. Windows rename also rejects an existing ZIP.
    staged_archive = final.parent / (final.name + ".zip")
    package_distribution(final, staged_archive)
    staged_archive.rename(archive)
    print(json.dumps({"directory": str(final), "zip": str(archive), "zip_bytes": archive.stat().st_size,
                      "distribution": inventory(final), "compilation": summaries}, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
