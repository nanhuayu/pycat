---
name: presentation
description: Create editable PPTX presentations from local content and assets, validate slide layouts, and inspect existing PPTX files with bundled scripts.
---

# Presentation

Use this skill for editable PowerPoint deliverables or bounded inspection of existing PPTX. Use the user's language and preferred style. The script builds native text, shapes, tables and charts; it does not generate content with a second model.

Read [commands](references/commands.md) before the first execution. Read [components](references/components.md) when authoring slide sources; [design](references/design.md) gives optional composition guidance. Supporting code is in `scripts/ppt.py` and `scripts/pyppt/`; execute the entrypoint without loading the whole implementation into context.

Prepare a normal workspace project containing `slides/01.slide`, `slides/02.slide`, and local `assets/`. Keep an outline or design note when it helps iteration. The skill directory is a read-only method package, not a place for output or task state.

1. Run `doctor` through the currently available Python or Shell tool. It reports actual dependency availability. Use the loaded skill's entrypoint path to locate the script; do not assume a developer path or that a release EXE is ordinary Python. Missing execution permission or dependencies remain unavailable.
2. Write focused slide sources using existing file tools. Prepare images explicitly through available tools, then store verified local assets. The builder does not download URLs or call image models. Use real table/chart data and preserve source limitations.
3. Validate and build in one batch. Read warnings and errors; repair the affected sources. Existing output requires its current digest to request replacement. A failed build leaves the previous output intact.
4. Inspect the committed PPTX to check page coverage, content and notes. Layout measurements are estimates; native fonts, charts and final appearance still need visual review in an available viewer. No faithful raster preview is implied.
5. Deliver the committed file using `file__deliver`. Inspection receipts and script output are not file delivery. Report any incomplete content or unverified visual details.

The first version supports local execution, bounded inspection and new deck generation. It does not support legacy `.ppt`, arbitrary existing-deck editing, animation or online Office sessions. For SSH workspaces, the local skill path is not a remote path: use only an explicitly deployed script and verified remote dependencies, or explain the unavailable capability without silently switching hosts.
