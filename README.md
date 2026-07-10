# Map Conversion Notes

This folder contains a Source/VBSP map extraction and conversion pipeline for building JSON map data for the JS clone map format.

It handles Source/VBSP version 20 maps. The nav-based converters read nav mesh **versions 9 (CS:S era) and 16 (CS:GO era)**. Maps currently in the repo:

- `de_mirage_csgo` — CS:GO port of de Mirage (nav v16).
- `de_dust2` — CS:S `de_dust2_unlimited` (nav v9).
- `de_dust2_winter` — winter-themed de Dust2 (nav v9).

Each map has a `source/<map>/SOURCE.md` with provenance.

## Layout

```text
tools/                      # extraction + conversion + validation + preview scripts
tests/                      # accuracy regression tests
source/<map>/               # original inputs: .bsp, .nav, SOURCE.md (provenance)
build/<map>/
  extracted/                # extraction intermediates (entities, geometry, pakfile, ...)
  detailed/                 # one folder per generated variant, each with json + meta + svg + html
  flat/  flat_assets/  flat_exact/
  layered/  layered_assets/
  flat_grid_<size>/  flat_assets_grid_<size>/
  layered_grid_<size>/  layered_assets_grid_<size>/
```

`source/` is hand-provided input; `build/` is fully regenerable by the commands below. Extraction writes to `build/<map>/extracted/`, and the `--extracted` flag on the converters points there. Each converted variant lands in its own `build/<map>/<variant>/` folder as `<map>_<variant>.json` (plus `.meta.json` and rendered `.svg`/`.html`).

## Files

- `tools/extract_bsp_geometry.py` extracts BSP entities, brush geometry, displacements, static props, embedded pak resources, and metadata.
- `tools/convert_bsp_to_js_map.py` converts BSP brush geometry into the JS map schema with many solids and ramps.
- `tools/convert_bsp_to_js_map_flat.py` creates flat nav-based maps with optional assets, auto colors, scale controls, exact/snap/grid nav modes, and spawn-safe walls/assets.
- `tools/convert_nav_to_layered_js_map.py` creates simplified non-flat maps by quantizing nav areas into height layers, playable stairs where possible, walkable ramps where stairs would exceed the game hull limits, and full-height containment walls.
- `tools/validate_js_map.py` checks spawns against floors, walls, assets, optional sampled player-route connectivity, floor-edge containment, and roof coverage.
- `tools/render_js_map_preview.py` renders a top-down SVG debug preview using map colors, with roofs hidden unless requested.
- `tools/render_js_map_html.py` renders an interactive HTML/canvas preview with layer toggles, pan/zoom, and a roof toggle.
- `tools/score_js_map_variants.py` validates and ranks generated variants.
- `tools/measure_map_accuracy.py` measures footprint fidelity and position exactness against the source nav mesh.
- `tools/generate_map.sh` runs the whole pipeline for one source map or every source map into the `build/<map>/` layout, including grid-resolution variants.

## Coordinate Mapping

Source maps use `X/Y` as the horizontal plane and `Z` as vertical height.

The JS clone schema uses:

```text
[x, vertical_y, horizontal_z]
```

The detailed converter maps:

```text
Source [x, y, z] -> JS [x, z, y]
```

The flat converter maps:

```text
Source [x, y, z] -> JS [x, ground_y, y]
```

For the flat maps, all gameplay verticality is collapsed to `ground_y = 128`. Floors are emitted from `y = 96` to `y = 128` and `killY = 64`. Combat spawns use the game import schema `[x, z, yaw]`; the server resolves their vertical placement from map collision. This keeps the generated map above a default engine/world floor at `y = 0` and makes the kill plane trigger before players can land on that world floor.

## Generated Outputs

Each variant folder `build/<map>/<variant>/` holds `<map>_<variant>.json` (the map), `<map>_<variant>.meta.json` (parameters + provenance), and `<map>_<variant>.svg` / `.html` previews.

## Game Import

Generated JSON can be passed directly to the game map loader. The converters add
the required scene look, world `bounds`, per-team `buyzones`, and an empty `nav`
array for server-side bot graph generation. Dust maps export as the engine's
`sand` theme and the winter map exports as `ice`.

Traversal ramps are marked `walk: true` so the player mover treats them as
ground. Ramp raycasts and bot graph construction remain engine/server concerns:
the supplied client raycaster only tests box solids, and the server generates
the bot graph from the empty `nav` array.

| Variant folder | Purpose |
| --- | --- |
| `detailed/` | Detailed brush approximation with solids, ramps, bombsites, spawns, and ladders. |
| `flat/` | Efficient flat nav-grid map with floors and boundary walls. |
| `flat_assets/` | Flat nav-grid map plus simplified crates from props. |
| `flat_exact/` | Higher-fidelity exact-nav variant for comparison/debugging. |
| `layered/` | Simplified non-flat nav-layer map with terraced floors, stairs, and containment walls. |
| `layered_assets/` | Layered map plus simplified crates placed on matched nav height layers. |

Current counts (de_mirage_csgo):

```text
detailed:
  solids: 4553
  ramps: 1620
  T spawns: 17
  CT spawns: 14
  bombsites: 2
  ladders: 3

flat:
  solids: 150
  floor: 44
  wall: 106

flat with assets:
  solids: 300
  floor: 44
  wall: 106
  crate: 150

flat exact:
  solids: 769
  floor: 398
  wall: 371

layered:
  solids: 564
  floor: 211
  wall: 353
  ramps: 0

layered with assets:
  solids: 727
  floor: 211
  wall: 353
  crate: 163
  ramps: 0
```

All flat default outputs currently have 17 T spawns, 14 CT spawns, 2 bombsites, and 0 ramps/ladders. Layered outputs may include walkable ramps where the source slope cannot form playable stairs.

## Run Commands

Generate every canonical variant and the grid matrix for all available source maps:

```bash
tools/generate_map.sh --all
```

The default grid matrix is `64`, `96`, and `128` Source units. It creates
`flat_grid_<size>`, `flat_assets_grid_<size>`, `layered_grid_<size>`, and
`layered_assets_grid_<size>` alongside the canonical `detailed`, `flat`,
`flat_assets`, `flat_exact`, `layered`, and `layered_assets` folders. Use a
custom set when needed:

```bash
tools/generate_map.sh --all --grid-sizes 64,128
tools/generate_map.sh de_dust2 --grid-sizes 64
```

The batch generator leaves maps open to the sky. It does not infer indoor
regions from the nav mesh, so it never adds blanket roof slabs. Use the
individual converter's `--include-roof` option only for an explicitly enclosed
map or a future region-aware roof pass.

Extract BSP data:

```bash
python3 tools/extract_bsp_geometry.py \
  source/de_mirage_csgo/de_mirage_csgo.bsp \
  --nav source/de_mirage_csgo/de_mirage_csgo.nav \
  --out build/de_mirage_csgo/extracted \
  --pak all
```

Use `--pak all` when you want better asset bounds. The flat asset converter reads `.mdl` hull bounds when those files exist; otherwise it falls back to model-name heuristics. Use `--pak none` for a smaller extraction if you do not need model-bound assets.

Generate the detailed JSON map:

```bash
python3 tools/convert_bsp_to_js_map.py \
  source/de_mirage_csgo/de_mirage_csgo.bsp \
  --extracted build/de_mirage_csgo/extracted \
  --out build/de_mirage_csgo/detailed/de_mirage_csgo_detailed.json
```

Generate the efficient flat JSON map:

```bash
python3 tools/convert_bsp_to_js_map_flat.py \
  source/de_mirage_csgo/de_mirage_csgo.bsp \
  --extracted build/de_mirage_csgo/extracted \
  --out build/de_mirage_csgo/flat/de_mirage_csgo_flat.json
```

Generate the flat JSON map with simplified assets:

```bash
python3 tools/convert_bsp_to_js_map_flat.py \
  source/de_mirage_csgo/de_mirage_csgo.bsp \
  --extracted build/de_mirage_csgo/extracted \
  --out build/de_mirage_csgo/flat_assets/de_mirage_csgo_flat_assets.json \
  --include-assets
```

Generate the exact-nav comparison map:

```bash
python3 tools/convert_bsp_to_js_map_flat.py \
  source/de_mirage_csgo/de_mirage_csgo.bsp \
  --extracted build/de_mirage_csgo/extracted \
  --out build/de_mirage_csgo/flat_exact/de_mirage_csgo_flat_exact.json \
  --nav-floor-mode exact
```

Generate the simplified non-flat layered map:

```bash
python3 tools/convert_nav_to_layered_js_map.py \
  source/de_mirage_csgo/de_mirage_csgo.bsp \
  --extracted build/de_mirage_csgo/extracted \
  --out build/de_mirage_csgo/layered/de_mirage_csgo_layered.json
```

Generate the simplified non-flat layered map with assets:

```bash
python3 tools/convert_nav_to_layered_js_map.py \
  source/de_mirage_csgo/de_mirage_csgo.bsp \
  --extracted build/de_mirage_csgo/extracted \
  --out build/de_mirage_csgo/layered_assets/de_mirage_csgo_layered_assets.json \
  --include-assets
```

## Useful Options

- `--scale 0.5` scales the emitted map coordinates and collision sizes.
- `--color-mode auto|fixed` controls generated colors. `auto` samples BSP material reflectivity and material names; colors are written as decimal `0xRRGGBB` integers in JSON.
- `--ground-y 128` controls the flat playable floor top height.
- `--kill-y-below-ground 64` places `killY` below the playable floor but above the default world floor.
- `--nav-floor-mode grid|snap|exact` controls flat nav geometry. `grid` is smallest, `exact` preserves nav cuts, and `snap` is a middle ground.
- `--nav-cell-size 128` controls grid simplification.
- `--nav-snap-size 64` controls snapped-nav simplification.
- `--boundary-wall-thickness 32` controls generated wall-strip thickness.
- `--min-wall-length 64` removes tiny generated wall segments.
- `--no-containment-seal-walls` disables the extra wall strips that seal partial cells at the validation grid boundary.
- `--containment-seal-grid-size 64`, `--containment-seal-edge-depth 16`, and `--containment-seal-wall-thickness` tune those seal strips.
- `--include-roof` adds thin horizontal roof slabs over every generated floor rectangle. It is off in the batch generator because nav data cannot determine which regions are indoors.
- `--roof-height 160` controls the roof bottom height above `ground_y`; by default it matches `--blocker-height` for flat maps.
- `--roof-thickness 32` controls roof slab thickness.
- `--roof-padding 0` expands or shrinks roof slab footprints.
- `--include-assets` adds simplified prop boxes.
- `--asset-density low|medium|high|full` controls the default static prop limit.
- `--asset-types crate` filters asset output types. Pillars are not supported: narrow upright props are emitted as crates so their vertical position is preserved.
- `--asset-bounds auto|heuristic|mdl` uses `.mdl` hull bounds for known asset models. `mdl` explicitly accepts unknown MDL models; `auto` is conservative to avoid decorative sky/roof props becoming collision boxes.
- `--asset-height-tolerance 96` rejects props whose Source vertical origin is too far from the local nav floor height.
- `--asset-height-search-distance 128` controls nearby nav lookup for the asset height filter.
- `--spawn-clearance 48` prevents generated asset boxes from colliding with spawns.
- `--spawn-wall-clearance 16` removes generated wall strips too close to spawns.
- `--player-radius 32` controls asset route-protection clearance during conversion.
- `--max-asset-nav-cells 6` rejects oversized asset boxes that would cover too much nav area.
- `--no-protect-nav-connectivity` disables route-preserving asset placement.
- Combat spawns are emitted as `[x, z, yaw]`, matching the game import schema. `--include-spawn-yaw` additionally emits a top-level `spawnYaws` object.

Layered converter options:

- `--height-step 64` controls vertical layer quantization. Larger values reduce complexity.
- `--nav-cell-size 128` controls horizontal floor simplification.
- `--min-ground-y 128` raises the lowest layered floor top above the default world floor.
- `--kill-y-below-ground 64` places `killY` below the lowest floor top but above the default world floor.
- `--spawn-height` is retained for the legacy `surfStart` compatibility field; it does not alter combat spawn coordinates.
- `--include-roof` adds a roof slab over each layer floor rectangle.
- `--roof-height 144` controls roof bottom height above each layer; by default it matches `--wall-height`.
- `--no-global-containment-walls` disables the extra tall outer wall ring around the combined floor/slope footprint.
- By default, the converter emits a lowest-layer foundation across the projected nav footprint and extends every structural wall to that foundation. `--no-foundation` restores floating layers for debugging only.
- `--slope-mode auto|stairs|ramps` controls how clear sloped nav areas are emitted. `auto` is the default: it emits stairs only when their risers and treads fit the game movement hull, otherwise it emits `walk: true` ramps. `stairs` is an explicit visual override and can create impractical treads.
- `--ramp-min-rise 28` and `--ramp-max-rise 192` control which sloped nav areas become stairs or ramps.
- `--ramp-wall-clearance 128` keeps enough layer boundary wall clear of each slope for route continuity. The separate full-height outer containment ring closes the map perimeter.
- `--min-wall-opening 32` closes collinear wall gaps at or below the 32-unit player hull width. Wider openings remain available as routes.
- `--stair-step-height 16` controls the target rise per stair step.
- `--stair-min-steps 2` and `--stair-max-steps 16` clamp stair segmentation.
- `--stair-min-tread-depth 32` requires a full player-hull-width tread in `auto` mode.
- Stair side walls are disabled by default to avoid narrow, rail-like wall artifacts. `--stair-side-walls` enables them for debugging.
- `--player-height 72`, `--player-step-height 18`, and `--player-radius 16` match the supplied game engine's standing hull. Roof exports require at least the configured standing clearance.
- `--no-containment-seal-walls` disables the final sampled edge-seal pass.
- `--containment-seal-grid-size 64` matches the default containment validator sample grid.
- `--containment-seal-edge-depth 16` controls how deep sampled edge probes look for existing blockers.
- `--containment-seal-wall-thickness` overrides the seal-wall strip thickness. By default it uses the smaller of wall thickness and edge depth.
- `--include-assets` adds simplified prop boxes on matched nav height layers.
- `--no-walls` emits only floors, foundation, stairs, and roof slabs.

## Validation And Preview

Validate a generated map:

```bash
python3 tools/validate_js_map.py \
  build/de_mirage_csgo/flat_assets/de_mirage_csgo_flat_assets.json \
  --check-connectivity \
  --check-containment \
  --world-floor-y 0 \
  --connectivity-grid-size 64 \
  --containment-grid-size 64 \
  --player-radius 24
```

Render a top-down SVG preview:

```bash
python3 tools/render_js_map_preview.py \
  build/de_mirage_csgo/flat_assets/de_mirage_csgo_flat_assets.json \
  --out build/de_mirage_csgo/flat_assets/de_mirage_csgo_flat_assets.svg
```

Render an interactive HTML preview:

```bash
python3 tools/render_js_map_html.py \
  build/de_mirage_csgo/flat_assets/de_mirage_csgo_flat_assets.json \
  --out build/de_mirage_csgo/flat_assets/de_mirage_csgo_flat_assets.html
```

Score generated variants:

```bash
python3 tools/score_js_map_variants.py \
  build/de_mirage_csgo/extracted/variants/*.json \
  --out build/de_mirage_csgo/extracted/variants/de_mirage_variant_scores.json
```

The latest route validation for the efficient asset map reports:

```text
ok: true
spawn floor misses: 0
spawn wall hits: 0
spawn asset hits: 0
route anchors reached: 33 / 33
open floor edges: 0
world floor check: floor top 128, killY 64
```

The exact-nav wall variant is kept as a high-fidelity debug reference. It is point-valid, but the sampled route and containment validators flag it as too fragmented/open for a 24-unit player radius.

Layered stair maps validate for spawn/floor/wall placement, containment, and world-floor safety. Their floor graph connects all route anchors at zero-radius sampling; the 24-unit route check remains conservative for tight layered wall clearances.

## Accuracy

The conversion gets **positions** exact (spawns, bombsites, and all horizontal
coordinates are 1:1 with the source map) but approximates **surfaces**. Measured
against the source nav mesh as ground truth:

| Variant | Invented walkable floor | Lost floor | Verticality | 24u connectivity |
| --- | --- | --- | --- | --- |
| flat (`grid`, default 128u) | +52% | 0% | flattened | 33/33 |
| flat (`grid` 64u) | +29% | 0% | flattened | 33/33 |
| flat (`exact`) | 0% | 0% | flattened | 18/33 (unplayable) |
| layered (default 128u) | +51% | 0% | preserved (84 levels) | 31/33 |

Key points for playable-geometry use:

- The dominant error is the **nav grid snap**, not the flattening: it rounds each
  nav area out to the grid, inventing floor between and around areas. Lowering
  `--nav-cell-size` (e.g. `64`) roughly halves it while still passing validators.
- `--nav-floor-mode exact` has a perfect footprint but is **not playable**: sub-48u
  nav slivers cannot fit a 24u player, leaving the map disconnected (18/33) with
  hundreds of unsealed edges. Footprint-perfect and playable are in tension.
- The detailed brush map (`convert_bsp_to_js_map.py`) keeps true 3D but the schema
  is axis-aligned boxes only; ~53% of Mirage's brushes are angled and are inflated
  ~56% in volume by the bounding-box approximation.
- The layered map preserves verticality but is currently only **31/33** connected
  for a 24u player (two anchors unreachable through tight layered wall clearances).

`tools/measure_map_accuracy.py` reports these metrics for any map against the
`.nav` + `entities.json` ground truth (no `.bsp` needed):

```bash
python3 tools/measure_map_accuracy.py \
  build/de_mirage_csgo/flat_assets/de_mirage_csgo_flat_assets.json \
  --nav source/de_mirage_csgo/de_mirage_csgo.nav \
  --entities build/de_mirage_csgo/extracted/entities.json
```

The `tests/` suite guards these properties against regression (invariants like
zero lost floor and exact positions are hard failures; known gaps like invented
floor and layered connectivity are baseline ceilings). It runs without the BSP:

```bash
python3 -m unittest discover -s tests
```

## Spawn Notes

Spawns are read directly from BSP entities:

- `info_player_terrorist`
- `info_player_counterterrorist`

The flat maps preserve Source `x/y` placement and only flatten vertical height. The first few converted flat spawns are:

```json
{
  "t": [[1376, 176, -304], [1376, 176, -208], [1376, 176, -112]],
  "ct": [[-1776, 176, -1976], [-2022, 176, -1818], [-2022, 176, -1978]]
}
```
