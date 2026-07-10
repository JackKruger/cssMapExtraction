#!/usr/bin/env python3
"""Measure how faithfully a generated JS map matches the source nav mesh.

Unlike ``validate_js_map.py`` (which checks a map is internally consistent and
playable), this compares a generated map against ground truth:

- floor footprint vs the source nav-area union (invented / lost walkable area),
- vertical collapse inherent in the source at the chosen grid resolution,
- spawn / bombsite positions vs the source BSP entities.

Ground truth comes from the ``.nav`` file and ``entities.json`` extraction, both
of which are small and version-controlled, so this runs without the large
``.bsp``. Emits JSON metrics; use the thresholds in ``tests/`` to gate them.
"""

from __future__ import annotations

import argparse
import json
import math
from pathlib import Path

from convert_bsp_to_js_map_flat import parse_nav_area_rects


def raster_rects(rects, fine):
    """Rasterize [min_x, min_z, max_x, max_z] rects into a set of fine cells."""
    cells = set()
    for min_x, min_z, max_x, max_z in rects:
        for ix in range(math.floor(min_x / fine), math.ceil(max_x / fine)):
            for iz in range(math.floor(min_z / fine), math.ceil(max_z / fine)):
                cells.add((ix, iz))
    return cells


def nav_footprint_cells(nav_areas, fine):
    return raster_rects([area["rect"] for area in nav_areas], fine)


def map_floor_rects(map_data):
    return [
        [solid["min"][0], solid["min"][2], solid["max"][0], solid["max"][2]]
        for solid in map_data.get("solids", [])
        if solid.get("type") == "floor"
    ]


def footprint_accuracy(map_data, nav_areas, fine):
    truth = nav_footprint_cells(nav_areas, fine)
    floor = raster_rects(map_floor_rects(map_data), fine)
    if not truth:
        return {"truth_cells": 0}
    invented = len(floor - truth)
    lost = len(truth - floor)
    return {
        "fine": fine,
        "truth_cells": len(truth),
        "floor_cells": len(floor),
        "invented_floor_pct": round(100 * invented / len(truth), 2),
        "lost_floor_pct": round(100 * lost / len(truth), 2),
    }


def vertical_collapse(nav_areas, cell, story=64.0):
    """Fraction of the footprint where areas > `story` apart share an XY cell."""
    grid = {}
    for area in nav_areas:
        min_x, min_z, max_x, max_z = area["rect"]
        h = (area["nw"][2] + area["se"][2] + area["ne_z"] + area["sw_z"]) / 4.0
        for ix in range(math.floor(min_x / cell), math.ceil(max_x / cell)):
            for iz in range(math.floor(min_z / cell), math.ceil(max_z / cell)):
                grid.setdefault((ix, iz), []).append(h)
    total = len(grid) or 1
    stacked = sum(1 for hs in grid.values() if max(hs) - min(hs) > story)
    worst = max((max(hs) - min(hs) for hs in grid.values()), default=0.0)
    return {
        "cell": cell,
        "footprint_cells": total,
        "stacked_pct": round(100 * stacked / total, 2),
        "worst_collapse_units": round(worst, 1),
    }


def distinct_floor_heights(map_data, tol=1.0):
    tops = sorted({round(s["max"][1] / tol) * tol for s in map_data.get("solids", []) if s.get("type") == "floor"})
    return len(tops)


def _spawn_entities(entities, classname):
    return [e for e in entities if e.get("classname") == classname and "origin" in e]


def position_exactness(map_data, entities, flat):
    """Do map spawn/site positions match the source entities exactly (X/Z)?

    ``flat`` maps use source [x, y] -> js [x, z]; detailed maps use the same
    horizontal mapping, so only the horizontal pair is compared here.
    """
    result = {}
    for team, classname in (("t", "info_player_terrorist"), ("ct", "info_player_counterterrorist")):
        src = _spawn_entities(entities, classname)
        got = map_data.get("spawns", {}).get(team, [])
        mismatches = 0
        for entity, spawn in zip(src, got):
            ox, oy = (float(v) for v in entity["origin"].split()[:2])
            # Combat spawns use the game schema [x, z, yaw].
            if len(spawn) >= 3 and not (math.isclose(ox, spawn[0], abs_tol=0.5) and math.isclose(oy, spawn[1], abs_tol=0.5)):
                mismatches += 1
        result[team] = {
            "source": len(src),
            "map": len(got),
            "count_match": len(src) == len(got),
            "position_mismatches": mismatches,
        }
    result["all_exact"] = all(
        r["count_match"] and r["position_mismatches"] == 0 for r in result.values() if isinstance(r, dict)
    )
    return result


def measure(map_path: Path, nav_path: Path, entities_path: Path, fine: int, cell: int, flat: bool):
    map_data = json.loads(map_path.read_text(encoding="utf-8"))
    nav_areas = parse_nav_area_rects(nav_path)["areas"]
    entities = json.loads(entities_path.read_text(encoding="utf-8"))["entities"]
    return {
        "map": str(map_path),
        "footprint": footprint_accuracy(map_data, nav_areas, fine),
        "source_vertical_collapse": vertical_collapse(nav_areas, cell),
        "distinct_floor_heights": distinct_floor_heights(map_data),
        "positions": position_exactness(map_data, entities, flat),
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("map", type=Path)
    parser.add_argument("--nav", type=Path, required=True)
    parser.add_argument("--entities", type=Path, required=True, help="Path to entities.json from extraction.")
    parser.add_argument("--fine", type=int, default=8, help="Raster resolution for footprint comparison.")
    parser.add_argument("--collapse-cell", type=int, default=64, help="Grid size for vertical-collapse stat.")
    parser.add_argument("--detailed", action="store_true", help="Map is the detailed (non-flat) variant.")
    args = parser.parse_args()
    report = measure(args.map, args.nav, args.entities, args.fine, args.collapse_cell, flat=not args.detailed)
    print(json.dumps(report, indent=2))


if __name__ == "__main__":
    main()
