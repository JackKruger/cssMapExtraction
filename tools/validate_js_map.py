#!/usr/bin/env python3
"""Validate JS-clone map JSON for common conversion issues."""

from __future__ import annotations

import argparse
import collections
import json
import math
from pathlib import Path
import sys


def point_in_solid_xz(x, z, solid, pad=0.0):
    return (
        solid["min"][0] - pad <= x <= solid["max"][0] + pad
        and solid["min"][2] - pad <= z <= solid["max"][2] + pad
    )


def solid_rect_xz(solid, pad=0.0):
    return [
        solid["min"][0] - pad,
        solid["min"][2] - pad,
        solid["max"][0] + pad,
        solid["max"][2] + pad,
    ]


def ramp_rect_xz(ramp, pad=0.0):
    return [
        ramp["min"][0] - pad,
        ramp["min"][2] - pad,
        ramp["max"][0] + pad,
        ramp["max"][2] + pad,
    ]


def walkable_ramp_surfaces(ramps):
    """Return ramp footprints as ground surfaces for 2D map validation."""
    return [
        {
            "min": [ramp["min"][0], min(ramp["yMin"], ramp["yMax"]), ramp["min"][2]],
            "max": [ramp["max"][0], max(ramp["yMin"], ramp["yMax"]), ramp["max"][2]],
            "type": "floor",
        }
        for ramp in ramps
        if ramp.get("walk") is True
    ]


def rects_overlap(a, b):
    return a[0] < b[2] and a[2] > b[0] and a[1] < b[3] and a[3] > b[1]


def is_roof_like(solid, max_thickness=48.0, min_footprint=1.0):
    if solid.get("type") != "wall":
        return False
    dx = solid["max"][0] - solid["min"][0]
    dy = solid["max"][1] - solid["min"][1]
    dz = solid["max"][2] - solid["min"][2]
    return 0 < dy <= max_thickness and dx >= min_footprint and dz >= min_footprint


def rect_center(rect):
    return [(rect["min"][0] + rect["max"][0]) * 0.5, (rect["min"][1] + rect["max"][1]) * 0.5]


def bounds_for_solids(solids):
    if not solids:
        return [-512, -512, 512, 512]
    return [
        min(solid["min"][0] for solid in solids),
        min(solid["min"][2] for solid in solids),
        max(solid["max"][0] for solid in solids),
        max(solid["max"][2] for solid in solids),
    ]


def point_in_any(x, z, solids, pad=0.0):
    return any(point_in_solid_xz(x, z, solid, pad) for solid in solids)


def build_walk_grid(floors, obstacles, grid_size, player_radius):
    min_x, min_z, max_x, max_z = bounds_for_solids(floors)
    ix0 = math.floor(min_x / grid_size)
    iz0 = math.floor(min_z / grid_size)
    ix1 = math.ceil(max_x / grid_size)
    iz1 = math.ceil(max_z / grid_size)
    cells = set()
    for ix in range(ix0, ix1):
        for iz in range(iz0, iz1):
            x = (ix + 0.5) * grid_size
            z = (iz + 0.5) * grid_size
            if not point_in_any(x, z, floors):
                continue
            if point_in_any(x, z, obstacles, player_radius):
                continue
            cells.add((ix, iz))
    return cells


def nearest_walk_cell(point, cells, grid_size):
    if not cells:
        return None
    x, z = point
    direct = (math.floor(x / grid_size), math.floor(z / grid_size))
    if direct in cells:
        return direct
    best = None
    for cell in cells:
        cx = (cell[0] + 0.5) * grid_size
        cz = (cell[1] + 0.5) * grid_size
        dist2 = (cx - x) ** 2 + (cz - z) ** 2
        if best is None or dist2 < best[0]:
            best = (dist2, cell)
    return best[1] if best else None


def connected_cells(cells, anchors):
    goals = [cell for cell in anchors if cell in cells]
    if len(goals) <= 1:
        return True, len(goals), 0
    goal_set = set(goals)
    queue = collections.deque([goals[0]])
    seen = {goals[0]}
    while queue:
        ix, iz = queue.popleft()
        for candidate in ((ix - 1, iz), (ix + 1, iz), (ix, iz - 1), (ix, iz + 1)):
            if candidate not in cells or candidate in seen:
                continue
            seen.add(candidate)
            queue.append(candidate)
    reached = len(goal_set & seen)
    return reached == len(goal_set), len(goal_set), reached


def edge_probe(ix, iz, dx, dz, grid_size, depth):
    x0 = ix * grid_size
    z0 = iz * grid_size
    x1 = (ix + 1) * grid_size
    z1 = (iz + 1) * grid_size
    if dx < 0:
        return [x0 - depth, z0, x0 + depth, z1], "west", z0, z1
    if dx > 0:
        return [x1 - depth, z0, x1 + depth, z1], "east", z0, z1
    if dz < 0:
        return [x0, z0 - depth, x1, z0 + depth], "north", x0, x1
    return [x0, z1 - depth, x1, z1 + depth], "south", x0, x1


def covered_fraction(edge_rect, side, span_start, span_end, blockers):
    spans = []
    horizontal = side in {"north", "south"}
    for blocker in blockers:
        if not rects_overlap(edge_rect, blocker):
            continue
        if horizontal:
            start = max(span_start, blocker[0])
            end = min(span_end, blocker[2])
        else:
            start = max(span_start, blocker[1])
            end = min(span_end, blocker[3])
        if end > start:
            spans.append((start, end))
    if not spans:
        return 0.0
    spans.sort()
    merged = []
    start, end = spans[0]
    for next_start, next_end in spans[1:]:
        if next_start <= end:
            end = max(end, next_end)
            continue
        merged.append((start, end))
        start, end = next_start, next_end
    merged.append((start, end))
    covered = sum(end - start for start, end in merged)
    return covered / max(span_end - span_start, 1e-6)


def validate_containment(
    floors,
    walls,
    roofs,
    assets,
    ramps,
    grid_size,
    player_radius,
    edge_depth,
    require_roof,
):
    floor_cells = build_walk_grid(floors, [], grid_size, 0.0)
    walk_cells = build_walk_grid(floors, walls + assets, grid_size, player_radius)
    wall_rects = [solid_rect_xz(wall) for wall in walls]
    ramp_rects = [ramp_rect_xz(ramp, grid_size * 2.0) for ramp in ramps]
    edge_blockers = wall_rects + ramp_rects
    roof_missing = []
    open_edges = []
    roof_cells_checked = 0

    for ix, iz in sorted(floor_cells):
        x = (ix + 0.5) * grid_size
        z = (iz + 0.5) * grid_size
        if require_roof:
            roof_cells_checked += 1
            if not any(point_in_solid_xz(x, z, roof) for roof in roofs):
                roof_missing.append({"cell": [ix, iz], "point": [round(x, 3), round(z, 3)]})
        for dx, dz in ((-1, 0), (1, 0), (0, -1), (0, 1)):
            if (ix + dx, iz + dz) in floor_cells:
                continue
            probe, side, span_start, span_end = edge_probe(ix, iz, dx, dz, grid_size, edge_depth)
            if covered_fraction(probe, side, span_start, span_end, edge_blockers) < 0.85:
                open_edges.append(
                    {
                        "cell": [ix, iz],
                        "side": side,
                        "edge": [round(value, 3) for value in probe],
                    }
                )

    return {
        "ok": not open_edges and (not require_roof or not roof_missing),
        "grid_size": grid_size,
        "player_radius": player_radius,
        "edge_depth": edge_depth,
        "walk_cells": len(walk_cells),
        "floor_cells": len(floor_cells),
        "open_edges": len(open_edges),
        "roof_required": require_roof,
        "roof_cells_checked": roof_cells_checked,
        "roof_missing_cells": len(roof_missing),
        "open_edge_samples": open_edges[:20],
        "roof_missing_samples": roof_missing[:20],
    }


def validate_connectivity(map_data, floors, obstacles, grid_size, player_radius):
    cells = build_walk_grid(floors, obstacles, grid_size, player_radius)
    anchor_points = []
    for team_spawns in map_data.get("spawns", {}).values():
        # Game combat spawns are [x, z, yaw].
        anchor_points.extend((spawn[0], spawn[1]) for spawn in team_spawns if len(spawn) >= 3)
    for site in map_data.get("bombsites", []):
        if "min" in site and "max" in site:
            anchor_points.append(((site["min"][0] + site["max"][0]) * 0.5, (site["min"][1] + site["max"][1]) * 0.5))
    anchors = [nearest_walk_cell(point, cells, grid_size) for point in anchor_points]
    anchors = [anchor for anchor in anchors if anchor is not None]
    ok, anchor_count, reached = connected_cells(cells, anchors)
    return {
        "ok": ok,
        "grid_size": grid_size,
        "player_radius": player_radius,
        "walk_cells": len(cells),
        "anchors": anchor_count,
        "anchors_reached": reached,
    }


def validate_map(
    map_data,
    wall_clearance,
    asset_clearance,
    require_spawn_yaws,
    check_connectivity,
    connectivity_grid_size,
    player_radius,
    check_containment=False,
    containment_grid_size=64.0,
    containment_edge_depth=16.0,
    require_roof=False,
    roof_thickness_max=48.0,
    world_floor_y=None,
):
    solids = map_data.get("solids", [])
    floors = [solid for solid in solids if solid.get("type") == "floor"]
    roof_min_footprint = 1.0
    roofs = [solid for solid in solids if is_roof_like(solid, roof_thickness_max, roof_min_footprint)]
    roof_ids = {id(solid) for solid in roofs}
    walls = [solid for solid in solids if solid.get("type") == "wall" and id(solid) not in roof_ids]
    assets = [solid for solid in solids if solid.get("type") in {"crate", "pillar"}]
    obstacles = walls + assets
    ramps = map_data.get("ramps", [])
    walkable_surfaces = floors + walkable_ramp_surfaces(ramps)
    spawns = map_data.get("spawns", {})

    errors = []
    warnings = []
    checked_spawns = 0
    for team in ("t", "ct"):
        team_spawns = spawns.get(team, [])
        for index, spawn in enumerate(team_spawns):
            checked_spawns += 1
            if len(spawn) < 3:
                errors.append({"kind": "bad_spawn", "team": team, "index": index, "spawn": spawn})
                continue
            x, z, _yaw = spawn[:3]
            if not any(point_in_solid_xz(x, z, floor) for floor in walkable_surfaces):
                errors.append({"kind": "spawn_off_floor", "team": team, "index": index, "spawn": spawn})
            if any(point_in_solid_xz(x, z, wall, wall_clearance) for wall in walls):
                errors.append({"kind": "spawn_hits_wall", "team": team, "index": index, "spawn": spawn})
            if any(point_in_solid_xz(x, z, asset, asset_clearance) for asset in assets):
                errors.append({"kind": "spawn_hits_asset", "team": team, "index": index, "spawn": spawn})

    if require_spawn_yaws:
        spawn_yaws = map_data.get("spawnYaws")
        if not spawn_yaws:
            errors.append({"kind": "missing_spawn_yaws"})
        else:
            for team in ("t", "ct"):
                if len(spawn_yaws.get(team, [])) != len(spawns.get(team, [])):
                    errors.append(
                        {
                            "kind": "spawn_yaw_count_mismatch",
                            "team": team,
                            "spawns": len(spawns.get(team, [])),
                            "yaws": len(spawn_yaws.get(team, [])),
                        }
                    )

    for site in map_data.get("bombsites", []):
        if "min" not in site or "max" not in site:
            warnings.append({"kind": "bad_bombsite", "site": site})
            continue
        x, z = rect_center(site)
        if walkable_surfaces and not any(point_in_solid_xz(x, z, floor) for floor in walkable_surfaces):
            warnings.append({"kind": "bombsite_center_off_floor", "site": site.get("name"), "center": [x, z]})

    surf_start = map_data.get("surfStart", [])
    if len(surf_start) >= 3 and walkable_surfaces:
        x, _y, z = surf_start[:3]
        if not any(point_in_solid_xz(x, z, floor) for floor in walkable_surfaces):
            warnings.append({"kind": "surf_start_off_floor", "surfStart": surf_start})

    world_floor = None
    if world_floor_y is not None:
        floor_tops = [floor["max"][1] for floor in floors]
        min_floor_top = min(floor_tops) if floor_tops else None
        kill_y = map_data.get("killY")
        world_floor = {
            "world_floor_y": world_floor_y,
            "min_floor_top": min_floor_top,
            "killY": kill_y,
            "floor_above_world": min_floor_top is not None and min_floor_top > world_floor_y,
            "kill_above_world": isinstance(kill_y, (int, float)) and kill_y > world_floor_y,
        }
        if min_floor_top is not None and min_floor_top <= world_floor_y:
            errors.append({"kind": "map_not_above_world_floor", **world_floor})
        if not isinstance(kill_y, (int, float)) or kill_y <= world_floor_y:
            errors.append({"kind": "kill_y_below_world_floor", **world_floor})

    connectivity = None
    if check_connectivity:
        connectivity = validate_connectivity(map_data, walkable_surfaces, obstacles, connectivity_grid_size, player_radius)
        if not connectivity["ok"]:
            errors.append({"kind": "connectivity_failed", **connectivity})

    containment = None
    if check_containment or require_roof:
        containment = validate_containment(
            floors,
            walls,
            roofs,
            assets,
            map_data.get("ramps", []),
            containment_grid_size,
            player_radius,
            containment_edge_depth,
            require_roof,
        )
        if containment["open_edges"]:
            errors.append({"kind": "containment_open_edges", **containment})
        if require_roof and containment["roof_missing_cells"]:
            errors.append({"kind": "roof_coverage_failed", **containment})

    return {
        "ok": not errors,
        "counts": {
            "solids": len(solids),
            "floors": len(floors),
            "walls": len(walls),
            "roofs": len(roofs),
            "assets": len(assets),
            "ramps": len(map_data.get("ramps", [])),
            "t_spawns": len(spawns.get("t", [])),
            "ct_spawns": len(spawns.get("ct", [])),
            "checked_spawns": checked_spawns,
            "bombsites": len(map_data.get("bombsites", [])),
            "ladders": len(map_data.get("ladders", [])),
        },
        "connectivity": connectivity,
        "containment": containment,
        "world_floor": world_floor,
        "errors": errors,
        "warnings": warnings,
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("map", type=Path)
    parser.add_argument("--wall-clearance", type=float, default=16.0)
    parser.add_argument("--asset-clearance", type=float, default=48.0)
    parser.add_argument("--require-spawn-yaws", action="store_true")
    parser.add_argument("--check-connectivity", action="store_true")
    parser.add_argument("--connectivity-grid-size", type=float, default=64.0)
    parser.add_argument("--player-radius", type=float, default=24.0)
    parser.add_argument("--check-containment", action="store_true")
    parser.add_argument("--containment-grid-size", type=float, default=64.0)
    parser.add_argument("--containment-edge-depth", type=float, default=16.0)
    parser.add_argument("--require-roof", action="store_true")
    parser.add_argument("--roof-thickness-max", type=float, default=48.0)
    parser.add_argument("--world-floor-y", type=float)
    args = parser.parse_args()

    map_data = json.loads(args.map.read_text(encoding="utf-8"))
    report = validate_map(
        map_data,
        args.wall_clearance,
        args.asset_clearance,
        args.require_spawn_yaws,
        args.check_connectivity,
        args.connectivity_grid_size,
        args.player_radius,
        args.check_containment,
        args.containment_grid_size,
        args.containment_edge_depth,
        args.require_roof,
        args.roof_thickness_max,
        args.world_floor_y,
    )
    print(json.dumps(report, indent=2))
    if not report["ok"]:
        sys.exit(1)


if __name__ == "__main__":
    main()
