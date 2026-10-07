# Script commands

The skill entrypoint returned by `skill__load` is `SKILL.md`. Its sibling `scripts/ppt.py` is the command entrypoint. It writes one JSON receipt to stdout and uses a nonzero exit code for failure. Run with the intended workspace as cwd; input, output and local asset paths must stay within that workspace/project.

## Python execution

For `python__exec`, use a short launcher with verified paths and command arguments:

```python
import runpy
import sys
sys.argv = [verified_script_path, "doctor"]
runpy.run_path(verified_script_path, run_name="__main__")
```

Replace `sys.argv` with the chosen command. `python__exec` uses the existing host runner, including the bundled worker in a release; it has a 60-second limit. Do not invoke `sys.executable -m pip` or assume `sys.executable script.py` is supported by a release EXE.

When an ordinary external Python interpreter has been verified, `shell__run` can use its `program/argv` interface. Long commands use the normal background process receipt and `shell__read`; do not repeatedly restart the build. An unavailable dependency needs environment provisioning or a verified interpreter, not repeated model-generated replacements of the engine.

## Commands

```text
ppt.py doctor
ppt.py validate deck
ppt.py build deck --output output/deck.pptx
ppt.py inspect output/deck.pptx --start-page 1 --page-count 5
ppt.py build deck --output output/deck.pptx --expected-digest VERIFIED_SHA256
```

`deck/slides/*.slide` is sorted by filename. Use zero-padded names for stable ordering. A project may instead keep `.slide` files directly in its root. One source file contains exactly one `Slide`; no project registry, manifest or generated plan database is required.

`build` returns path, digest, page count, size and warnings only after the temporary output is reopened and checked. A new path requires no digest. Replacing an existing file requires its exact current SHA-256; concurrent script builds are excluded with a short-lived lock, and a digest change during rendering prevents replacement. This conflict check is not an operating-system sandbox or a guarantee that unrelated editors respect the script's lock.

`inspect` uses 1-based page ranges, at most ten pages per call, bounded text/notes and at most 200 shape descriptors per page. Positions and dimensions are EMU. `next_page` continues the deck; `text_truncated` and `shapes_truncated` disclose incomplete views. It does not return original image bytes or a visual preview.

Budgets: 1–128 slides, 256 KiB per source, 10,000 elements and depth 64 per slide, local image bytes up to 16 MiB / 40 million pixels, and PPTX up to 20 MiB. ZIP entry and expanded-part budgets are checked before inspection. Large input is explicitly rejected rather than partially published.

Errors include `missing_dependencies`, `invalid_arguments`, `invalid_path`, `invalid_project`, `invalid_source`, `resource_limit`, `validation_failed`, `output_conflict`, `output_busy` and `operation_failed`. Read the diagnostic and fix its cause. Missing images, unsupported components, corrupt resources and off-canvas geometry must not become a successful placeholder deck.
