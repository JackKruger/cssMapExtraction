# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

A Python pipeline that converts Source-engine maps (Counter-Strike `.bsp` + `.nav`) into JSON map data for a separate JS clone game. Pure standard-library Python 3 (no dependencies, no package manifest). Everything is driven by CLI scripts in `tools/`.

## Layout

- `source/<map>/` — hand-provided inputs: `<map>.bsp`, `<map>.nav`, `SOURCE.md` (provenance). **`.bsp` files are gitignored** (large binaries); re-download per `SOURCE.md`.
- `build/<map>/` — fully regenerable output. `extracted/` holds extraction intermediates; each generated variant gets its own folder (`detailed/`, `flat/`, `flat_assets/`, `flat_exact/`, `layered/`, `layered_assets/`) containing `<map>_<variant>.{json,meta.json,svg,html}`.
- `tools/` — the pipeline scripts. `tests/` — accuracy regression tests.

## Commands

Run everything from the repo root. The full pipeline for one map:

```bash
tools/generate_map.sh <map-name>        # extract + all 6 variants + previews
FORCE_EXTRACT=1 tools/generate_map.sh <map-name>   # also re-run extraction
```

Individual stages (see README.md for the full flag set):

```bash
# 1. Extract (writes build/<map>/extracted/); --pak all needed for asset .mdl bounds
python3 tools/extract_bsp_geometry.py source/<map>/<map>.bsp --nav source/<map>/<map>.nav --out build/<map>/extracted --pak all
# 2. Convert (three converters; --extracted points at the extraction dir)
python3 tools/convert_bsp_to_js_map.py       <bsp> --extracted build/<map>/extracted --out build/<map>/detailed/<map>_detailed.json
python3 tools/convert_bsp_to_js_map_flat.py  <bsp> --extracted build/<map>/extracted --out build/<map>/flat/<map>_flat.json --include-roof
python3 tools/convert_nav_to_layered_js_map.py <bsp> --extracted build/<map>/extracted --out build/<map>/layered/<map>_layered.json --include-roof
# 3. Validate / preview / measure
python3 tools/validate_js_map.py <map.json> --check-connectivity --check-containment --require-roof --world-floor-y 0 --player-radius 24
python3 tools/render_js_map_preview.py <map.json> --out <out.svg>   # render_js_map_html.py for interactive HTML
python3 tools/measure_map_accuracy.py <map.json> --nav <nav> --entities build/<map>/extracted/entities.json
```

Tests (stdlib unittest; run without the `.bsp` — they use the tracked `.nav` + `entities.json`):

```bash
python3 -m unittest discover -s tests            # all
python3 -m unittest tests.test_map_accuracy.TestInvariants.test_flat_map_fully_connected   # single test
```

## Architecture

**Pipeline:** `extract` → one of three `convert`ers → `validate` / `render` / `measure` / `score`. The converters re-parse the `.bsp` directly for geometry/colors and read only a few small files from `extracted/` (`entities.json`, `geometry/models.json`, `geometry/static_props.json`); the large `geometry/*.jsonl` and `*.obj` dumps are debug-only and gitignored.

**Shared code lives in the flat converter.** `convert_bsp_to_js_map_flat.py` is the hub — the layered converter and the accuracy tools import helpers from it (`parse_nav_area_rects`, color palette, asset bounds, cell/rect merging). `extract_bsp_geometry.py` is imported by the converters for BSP lump parsing. When touching a shared helper, check its importers.

**Three converters, different fidelity trade-offs:**
- `convert_bsp_to_js_map.py` (**detailed**) — approximates each convex brush as an axis-aligned box + one-axis ramps. Keeps 3D but the schema is boxes-only, so angled geometry is inflated. Rough reference, not clean-validating.
- `convert_bsp_to_js_map_flat.py` (**flat**) — projects the nav-mesh walkable footprint onto one plane. `--nav-floor-mode grid|snap|exact`; `grid` (default) is the primary output. Falls back to BSP-face projection if no nav (`--flat-source faces`), which is much sparser.
- `convert_nav_to_layered_js_map.py` (**layered**) — quantizes nav areas into height layers with stairs; preserves verticality but shares the flat converter's horizontal grid snapping.

**Coordinate mapping:** Source is `X/Y` horizontal, `Z` vertical. Detailed maps `[x,y,z]→[x,z,y]`; flat maps `[x,y,z]→[x,ground_y,y]` (verticality collapsed to `ground_y=128`, spawns at 176, `killY=64`, above an assumed world floor at y=0). Spawns/bombsites are read straight from BSP entities and are **positionally exact** — only surfaces are approximated.

**Nav format:** `parse_nav_area_rects` supports nav **versions 9 (CS:S) and 16 (CS:GO)** via version-gated per-area field parsing. The two eras differ in attribute width, approach-area blocks, and light/visibility blocks — the gates are verified against real v9/v16 files only. `killY`, roof coverage, and containment are validated by `validate_js_map.py`.

## Critical constraints

- **Pillars are disabled by default.** The target JS engine can't raise `type: "pillar"` solids above `y=0` (engine bug). All three converters remap pillar→crate (identical box, working type) unless `--allow-pillars` is passed. Never introduce a code path that emits `type: "pillar"` by default.
- **Mirage (v16) output must stay byte-identical** when changing nav parsing — it's the regression anchor. Regenerate `de_mirage_csgo_flat_assets` and diff before/after any `parse_nav_area_rects` edit.
- **`--include-roof` seals the play volume** by capping every floor rect with a slab (emitted as `type: "wall"`); that's why previews hide roofs behind a toggle. Omit it for open maps.
- **`--nav-floor-mode exact`** has a perfect footprint but is unplayable (sub-48u slivers disconnect a 24u player). For playable geometry, lower `--nav-cell-size` (e.g. 64) instead — the default 128u grid snap invents ~50% extra walkable floor.

## Tests as accuracy guardrails

`tests/test_map_accuracy.py` splits assertions into hard invariants (zero lost floor, exact spawn/site positions, flat-is-flat, layered-is-vertical, flat fully connected) and documented regression ceilings (invented-floor %, layered connectivity) that fail if fidelity *degrades*. `measure_map_accuracy.py` is the reusable metric behind them, comparing generated floors against the nav-mesh ground truth.
