#!/usr/bin/env python3
"""Score generated JS map variants using validation and simple cost heuristics."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from validate_js_map import validate_map


def score_report(report, target_assets):
    counts = report["counts"]
    assets = counts["assets"]
    solids = counts["solids"]
    connectivity = report.get("connectivity") or {}
    score = 1000.0
    if not report["ok"]:
        score -= 10000.0
    if connectivity.get("ok"):
        score += 500.0
    score -= solids * 0.18
    score -= abs(assets - target_assets) * 0.35
    score -= len(report.get("warnings", [])) * 10.0
    return round(score, 3)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("maps", nargs="+", type=Path)
    parser.add_argument("--out", type=Path)
    parser.add_argument("--target-assets", type=int, default=180)
    parser.add_argument("--connectivity-grid-size", type=float, default=64.0)
    parser.add_argument("--player-radius", type=float, default=24.0)
    parser.add_argument("--check-ramp-connectivity", action="store_true")
    args = parser.parse_args()

    rows = []
    for path in args.maps:
        if path.name.endswith(".meta.json"):
            continue
        map_data = json.loads(path.read_text(encoding="utf-8"))
        if "solids" not in map_data or "spawns" not in map_data:
            continue
        has_ramps = bool(map_data.get("ramps"))
        report = validate_map(
            map_data,
            wall_clearance=16.0,
            asset_clearance=48.0,
            require_spawn_yaws=False,
            check_connectivity=args.check_ramp_connectivity or not has_ramps,
            connectivity_grid_size=args.connectivity_grid_size,
            player_radius=args.player_radius,
        )
        rows.append(
            {
                "path": str(path),
                "name": map_data.get("name", path.stem),
                "score": score_report(report, args.target_assets),
                "ok": report["ok"],
                "counts": report["counts"],
                "connectivity": report.get("connectivity"),
                "errors": report.get("errors", []),
                "warnings": report.get("warnings", []),
            }
        )
    rows.sort(key=lambda item: item["score"], reverse=True)
    output = {"variants": rows}
    text = json.dumps(output, indent=2)
    if args.out:
        args.out.parent.mkdir(parents=True, exist_ok=True)
        args.out.write_text(text + "\n", encoding="utf-8")
    print(text)


if __name__ == "__main__":
    main()
