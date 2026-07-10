#!/usr/bin/env python3
"""Create a simplified non-flat JS-clone map from Source nav areas.

This converter keeps limited vertical structure without using full BSP brush
geometry. It quantizes walkable nav areas into height layers, emits merged
axis-aligned floor slabs per layer, and converts clearly sloped nav areas into
playable stairs or walkable ramps when the game hull cannot use stairs.
"""

from __future__ import annotations

import argparse
import collections
import json
import math
from pathlib import Path

from convert_bsp_to_js_map_flat import (
    ASSET_DENSITY_LIMITS,
    add_engine_runtime_fields,
    add_rect_to_cells,
    asset_info_for_model_with_bounds,
    boundary_wall_rects,
    build_color_palette,
    build_bombsites,
    clean_num,
    clean_vec,
    filter_wall_rects,
    load_entities,
    load_models,
    merge_cells_to_rects,
    parse_nav_area_rects,
    parse_asset_types,
    parse_vec3,
    parse_yaw,
    rect_hits_spawn_clearance,
    rotated_footprint_size,
    solid_hits_spawn_clearance,
    source_to_combat_spawn,
)


# These match the supplied game engine's PHYS constants. Generated geometry must
# not assume a smaller player hull, a shorter player, or a taller step-up.
ENGINE_PLAYER_RADIUS = 16.0
ENGINE_PLAYER_HEIGHT = 72.0
ENGINE_STEP_HEIGHT = 18.0


def source_to_js(point):
    # Source is X/Y horizontal and Z vertical. JS uses X/Z horizontal and Y vertical.
    return [point[0], point[2], point[1]]


def build_spawns_layered(entities, nav_areas=None, height_step=64.0, layer_cells=None, nav_cell_size=128, spawn_height=48.0):
    spawns = {"t": [], "ct": []}
    spawn_yaws = {"t": [], "ct": []}
    spawn_entities = {"t": [], "ct": []}
    for entity in entities:
        classname = entity.get("classname")
        if classname == "info_player_terrorist" and "origin" in entity:
            spawn_entities["t"].append(entity)
        elif classname == "info_player_counterterrorist" and "origin" in entity:
            spawn_entities["ct"].append(entity)
    for team, team_entities in spawn_entities.items():
        for entity in team_entities:
            source = parse_vec3(entity["origin"])
            yaw = clean_num(parse_yaw(entity))
            spawns[team].append(source_to_combat_spawn(source, yaw))
            spawn_yaws[team].append(yaw)
    return spawns, spawn_yaws, spawn_entities


def spawn_clearance_points(spawn_entities):
    points = []
    for team_entities in spawn_entities.values():
        for entity in team_entities:
            source = parse_vec3(entity["origin"])
            points.append((source[0], source[1]))
    return points


def nav_heights(area):
    return [area["nw"][2], area["ne_z"], area["se"][2], area["sw_z"]]


def quantize_height(height, step):
    return round(height / step) * step


def area_height_at(area, x, z):
    min_x, min_z, max_x, max_z = area["rect"]
    width = max(max_x - min_x, 1e-6)
    depth = max(max_z - min_z, 1e-6)
    tx = max(0.0, min(1.0, (x - min_x) / width))
    tz = max(0.0, min(1.0, (z - min_z) / depth))
    h_nw, h_ne, h_se, h_sw = nav_heights(area)
    north = h_nw * (1.0 - tx) + h_ne * tx
    south = h_sw * (1.0 - tx) + h_se * tx
    return north * (1.0 - tz) + south * tz


def ramp_candidate(area, min_rise, max_rise):
    heights = nav_heights(area)
    span = max(heights) - min(heights)
    if span < min_rise or span > max_rise:
        return None

    h_nw, h_ne, h_se, h_sw = heights
    west = (h_nw + h_sw) * 0.5
    east = (h_ne + h_se) * 0.5
    north = (h_nw + h_ne) * 0.5
    south = (h_sw + h_se) * 0.5
    dx = east - west
    dz = south - north
    if abs(dx) >= abs(dz):
        if abs(dx) < min_rise or abs(dz) > abs(dx) * 0.65:
            return None
        axis = 0
        y_min, y_max = west, east
    else:
        if abs(dz) < min_rise or abs(dx) > abs(dz) * 0.65:
            return None
        axis = 2
        y_min, y_max = north, south

    min_x, min_z, max_x, max_z = area["rect"]
    return {
        "min": [min_x, min(heights), min_z],
        "max": [max_x, max(heights), max_z],
        "axis": axis,
        "yMin": y_min,
        "yMax": y_max,
    }


def rasterize_area(area, cell_size, height_step, layer_cells):
    min_x, min_z, max_x, max_z = area["rect"]
    ix0 = math.floor(min_x / cell_size)
    iz0 = math.floor(min_z / cell_size)
    ix1 = math.ceil(max_x / cell_size) - 1
    iz1 = math.ceil(max_z / cell_size) - 1
    for ix in range(ix0, ix1 + 1):
        for iz in range(iz0, iz1 + 1):
            cell_min_x = ix * cell_size
            cell_min_z = iz * cell_size
            cell_max_x = (ix + 1) * cell_size
            cell_max_z = (iz + 1) * cell_size
            if cell_max_x <= min_x or cell_min_x >= max_x or cell_max_z <= min_z or cell_min_z >= max_z:
                continue
            center_x = (max(cell_min_x, min_x) + min(cell_max_x, max_x)) * 0.5
            center_z = (max(cell_min_z, min_z) + min(cell_max_z, max_z)) * 0.5
            layer = quantize_height(area_height_at(area, center_x, center_z), height_step)
            layer_cells[layer].add((ix, iz))


def floor_solid(rect, layer, floor_thickness, color):
    min_x, min_z, max_x, max_z = rect
    return {
        "min": clean_vec([min_x, layer - floor_thickness, min_z]),
        "max": clean_vec([max_x, layer, max_z]),
        "color": color,
        "type": "floor",
    }


def wall_solid(rect, layer, wall_height, color, base_y=None):
    min_x, min_z, max_x, max_z = rect
    return {
        "min": clean_vec([min_x, layer if base_y is None else base_y, min_z]),
        "max": clean_vec([max_x, layer + wall_height, max_z]),
        "color": color,
        "type": "wall",
    }


def tall_wall_solid(rect, min_y, max_y, color):
    min_x, min_z, max_x, max_z = rect
    return {
        "min": clean_vec([min_x, min_y, min_z]),
        "max": clean_vec([max_x, max_y, max_z]),
        "color": color,
        "type": "wall",
    }


def roof_solid(rect, layer, roof_height, roof_thickness, color):
    min_x, min_z, max_x, max_z = rect
    roof_bottom = layer + roof_height
    return {
        "min": clean_vec([min_x, roof_bottom, min_z]),
        "max": clean_vec([max_x, roof_bottom + roof_thickness, max_z]),
        "color": color,
        "type": "wall",
    }


def ramp_to_output(ramp, floor_thickness, color):
    return {
        "min": clean_vec([ramp["min"][0], ramp["min"][1] - floor_thickness, ramp["min"][2]]),
        "max": clean_vec(ramp["max"]),
        "axis": ramp["axis"],
        "yMin": clean_num(ramp["yMin"]),
        "yMax": clean_num(ramp["yMax"]),
        "rot": 0,
        "walk": True,
        "color": color,
    }


def stair_solid(rect, bottom_y, top_y, color):
    min_x, min_z, max_x, max_z = rect
    return {
        "min": clean_vec([min_x, bottom_y, min_z]),
        "max": clean_vec([max_x, top_y, max_z]),
        "color": color,
        "type": "floor",
    }


def ramp_run_length(ramp):
    axis = ramp["axis"]
    return ramp["max"][axis] - ramp["min"][axis]


def usable_stair_steps(ramp, target_step_height, min_steps, max_steps, min_tread_depth, player_step_height):
    """Return a playable tread count, or None when this slope needs a ramp.

    A player-sized hull needs a full hull-width tread, and each riser must fit
    inside the game's step-up limit. Otherwise movement catches on the tread
    faces even when the visual staircase appears reasonable.
    """
    rise = abs(ramp["yMax"] - ramp["yMin"])
    max_riser = min(target_step_height, player_step_height)
    required_steps = max(min_steps, int(math.ceil(rise / max(max_riser, 1e-6))))
    max_steps_by_tread = int(math.floor((ramp_run_length(ramp) + 1e-6) / min_tread_depth))
    allowed_steps = min(max_steps, max_steps_by_tread)
    if allowed_steps < min_steps or required_steps > allowed_steps:
        return None
    return required_steps


def stair_solids_for_ramp(ramp, floor_thickness, steps, color):
    min_x, min_z = ramp["min"][0], ramp["min"][2]
    max_x, max_z = ramp["max"][0], ramp["max"][2]
    axis = ramp["axis"]
    y0 = ramp["yMin"]
    y1 = ramp["yMax"]
    low_y = min(y0, y1)
    bottom_y = low_y - floor_thickness
    solids = []
    rects = []
    for index in range(steps):
        t0 = index / steps
        t1 = (index + 1) / steps
        if axis == 0:
            x0 = min_x + (max_x - min_x) * t0
            x1 = min_x + (max_x - min_x) * t1
            rect = [x0, min_z, x1, max_z]
        else:
            z0 = min_z + (max_z - min_z) * t0
            z1 = min_z + (max_z - min_z) * t1
            rect = [min_x, z0, max_x, z1]
        # Use the high edge of each tread when ascending and the near edge when descending.
        height_t = t1 if y1 >= y0 else t0
        top_y = y0 + (y1 - y0) * height_t
        solids.append(stair_solid(rect, bottom_y, top_y, color))
        rects.append(rect)
    return solids, rects


def stair_side_walls_for_ramp(ramp, wall_thickness, wall_height, color):
    min_x, min_z = ramp["min"][0], ramp["min"][2]
    max_x, max_z = ramp["max"][0], ramp["max"][2]
    low_y = min(ramp["yMin"], ramp["yMax"])
    high_y = max(ramp["yMin"], ramp["yMax"])
    if ramp["axis"] == 0:
        rects = [
            [min_x, min_z - wall_thickness, max_x, min_z],
            [min_x, max_z, max_x, max_z + wall_thickness],
        ]
    else:
        rects = [
            [min_x - wall_thickness, min_z, min_x, max_z],
            [max_x, min_z, max_x + wall_thickness, max_z],
        ]
    return [tall_wall_solid(rect, low_y, high_y + wall_height, color) for rect in rects]


def walkable_vertical_bounds(layer_cells, stair_solids, ramps, floor_thickness):
    """Return the structural base and highest walkable surface for the map."""
    bottoms = [layer - floor_thickness for layer in layer_cells]
    tops = list(layer_cells)
    bottoms.extend(solid["min"][1] for solid in stair_solids)
    tops.extend(solid["max"][1] for solid in stair_solids)
    bottoms.extend(ramp["min"][1] for ramp in ramps)
    tops.extend(max(ramp["yMin"], ramp["yMax"]) for ramp in ramps)
    if not bottoms or not tops:
        return 0.0, 0.0
    return min(bottoms), max(tops)


def expand_rect(rect, amount):
    return [rect[0] - amount, rect[1] - amount, rect[2] + amount, rect[3] + amount]


def rects_overlap(a, b):
    return a[0] < b[2] and a[2] > b[0] and a[1] < b[3] and a[3] > b[1]


def solid_rect_xz(solid):
    return [solid["min"][0], solid["min"][2], solid["max"][0], solid["max"][2]]


def is_roof_like_solid(solid, max_thickness=48.0):
    if solid.get("type") != "wall":
        return False
    dx = solid["max"][0] - solid["min"][0]
    dy = solid["max"][1] - solid["min"][1]
    dz = solid["max"][2] - solid["min"][2]
    return 0 < dy <= max_thickness and dx > 0 and dz > 0


def point_in_solid_xz(x, z, solid):
    return solid["min"][0] <= x <= solid["max"][0] and solid["min"][2] <= z <= solid["max"][2]


def bounds_for_solids(solids):
    return [
        min(solid["min"][0] for solid in solids),
        min(solid["min"][2] for solid in solids),
        max(solid["max"][0] for solid in solids),
        max(solid["max"][2] for solid in solids),
    ]


def sample_floor_cells(floor_solids, grid_size):
    if not floor_solids or grid_size <= 0:
        return set()
    min_x, min_z, max_x, max_z = bounds_for_solids(floor_solids)
    ix0 = math.floor(min_x / grid_size)
    iz0 = math.floor(min_z / grid_size)
    ix1 = math.ceil(max_x / grid_size)
    iz1 = math.ceil(max_z / grid_size)
    cells = set()
    for ix in range(ix0, ix1):
        for iz in range(iz0, iz1):
            x = (ix + 0.5) * grid_size
            z = (iz + 0.5) * grid_size
            if any(point_in_solid_xz(x, z, floor) for floor in floor_solids):
                cells.add((ix, iz))
    return cells


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


def edge_wall_rect(ix, iz, dx, dz, grid_size, thickness):
    x0 = ix * grid_size
    z0 = iz * grid_size
    x1 = (ix + 1) * grid_size
    z1 = (iz + 1) * grid_size
    if dx < 0:
        return [x0 - thickness, z0, x0, z1]
    if dx > 0:
        return [x1, z0, x1 + thickness, z1]
    if dz < 0:
        return [x0, z0 - thickness, x1, z0]
    return [x0, z1, x1, z1 + thickness]


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


def merge_wall_rects(rects):
    groups = collections.defaultdict(list)
    for rect in rects:
        width = rect[2] - rect[0]
        depth = rect[3] - rect[1]
        if width >= depth:
            groups[("h", rect[1], rect[3])].append((rect[0], rect[2]))
        else:
            groups[("v", rect[0], rect[2])].append((rect[1], rect[3]))

    merged = []
    for key, spans in groups.items():
        spans.sort()
        start, end = spans[0]
        for next_start, next_end in spans[1:] + [(None, None)]:
            if next_start is not None and next_start <= end + 1e-4:
                end = max(end, next_end)
                continue
            if key[0] == "h":
                merged.append([start, key[1], end, key[2]])
            else:
                merged.append([key[1], start, key[2], end])
            start, end = next_start, next_end
    return merged


def close_narrow_wall_gaps(rects, max_opening):
    """Join collinear wall spans separated by an unusable player-sized gap."""
    if max_opening <= 0:
        return rects
    groups = collections.defaultdict(list)
    for rect in rects:
        width = rect[2] - rect[0]
        depth = rect[3] - rect[1]
        if width >= depth:
            groups[("h", rect[1], rect[3])].append((rect[0], rect[2]))
        else:
            groups[("v", rect[0], rect[2])].append((rect[1], rect[3]))

    closed = []
    for (axis, fixed_min, fixed_max), spans in groups.items():
        spans.sort()
        start, end = spans[0]
        for next_start, next_end in spans[1:] + [(None, None)]:
            if next_start is not None and next_start - end <= max_opening + 1e-4:
                end = max(end, next_end)
                continue
            if axis == "h":
                closed.append([start, fixed_min, end, fixed_max])
            else:
                closed.append([fixed_min, start, fixed_max, end])
            start, end = next_start, next_end
    return closed


def containment_seal_wall_rects(
    floor_solids,
    existing_wall_solids,
    ramps,
    grid_size,
    edge_depth,
    wall_thickness,
    min_wall_length,
    coverage_threshold=0.85,
):
    floor_cells = sample_floor_cells(floor_solids, grid_size)
    if not floor_cells:
        return []
    blockers = [solid_rect_xz(wall) for wall in existing_wall_solids]
    blockers.extend(
        [
            [
                ramp["min"][0] - grid_size * 2.0,
                ramp["min"][2] - grid_size * 2.0,
                ramp["max"][0] + grid_size * 2.0,
                ramp["max"][2] + grid_size * 2.0,
            ]
            for ramp in ramps
        ]
    )
    rects = []
    for ix, iz in sorted(floor_cells):
        for dx, dz in ((-1, 0), (1, 0), (0, -1), (0, 1)):
            if (ix + dx, iz + dz) in floor_cells:
                continue
            probe, side, span_start, span_end = edge_probe(ix, iz, dx, dz, grid_size, edge_depth)
            if covered_fraction(probe, side, span_start, span_end, blockers) < coverage_threshold:
                rects.append(edge_wall_rect(ix, iz, dx, dz, grid_size, wall_thickness))
    return filter_wall_rects(merge_wall_rects(rects), min_wall_length)


def wall_hits_ramp_clearance(rect, ramp_rects, clearance):
    if clearance <= 0:
        return False
    return any(rects_overlap(rect, expand_rect(ramp_rect, clearance)) for ramp_rect in ramp_rects)


def rect_distance_sq_to_point(rect, x, z):
    dx = 0.0
    if x < rect[0]:
        dx = rect[0] - x
    elif x > rect[2]:
        dx = x - rect[2]
    dz = 0.0
    if z < rect[1]:
        dz = rect[1] - z
    elif z > rect[3]:
        dz = z - rect[3]
    return dx * dx + dz * dz


def asset_layer_for_origin(origin, nav_areas, height_step, tolerance, search_distance, layer_cells, nav_cell_size):
    x, z, source_y = origin[0], origin[1], origin[2]
    containing = [area for area in nav_areas if area["rect"][0] <= x <= area["rect"][2] and area["rect"][1] <= z <= area["rect"][3]]
    candidates = containing
    if not candidates and search_distance >= 0:
        max_dist_sq = search_distance * search_distance
        best_dist = None
        for area in nav_areas:
            dist_sq = rect_distance_sq_to_point(area["rect"], x, z)
            if dist_sq > max_dist_sq:
                continue
            if best_dist is None or dist_sq < best_dist - 1e-6:
                best_dist = dist_sq
                candidates = [area]
            elif abs(dist_sq - best_dist) < 1e-6:
                candidates.append(area)
    if not candidates:
        return None, "outside_nav"

    best = None
    for area in candidates:
        nav_y = area_height_at(area, x, z)
        delta = source_y - nav_y
        layer = quantize_height(nav_y, height_step)
        score = abs(delta)
        if best is None or score < best[0]:
            best = (score, delta, layer)
    if best is None:
        return None, "outside_nav"
    if tolerance >= 0 and best[0] > tolerance:
        return None, "height_mismatch"

    ix = math.floor(x / nav_cell_size)
    iz = math.floor(z / nav_cell_size)
    layer = best[2]
    if (ix, iz) in layer_cells.get(layer, set()):
        return layer, None
    for dx in (-1, 0, 1):
        for dz in (-1, 0, 1):
            if (ix + dx, iz + dz) in layer_cells.get(layer, set()):
                return layer, None
    return None, "outside_layer"


def asset_solid_from_origin_layered(model, origin, yaw, layer, color_palette, extracted_dir, asset_bounds, allowed_types):
    asset_info = asset_info_for_model_with_bounds(model, extracted_dir, asset_bounds)
    if not asset_info:
        return None, "unmatched_model"
    if allowed_types is not None and asset_info["type"] not in allowed_types:
        return None, "filtered_type"
    size_x, size_z, height = asset_info["dims"]
    size_x, size_z = rotated_footprint_size(size_x, size_z, yaw)
    source_x, source_z = origin[0], origin[1]
    min_x = source_x - size_x * 0.5
    max_x = source_x + size_x * 0.5
    min_z = source_z - size_z * 0.5
    max_z = source_z + size_z * 0.5
    color = color_palette.get(asset_info["type"], color_palette.get("crate", 0x8D6039))
    return (
        {
            "min": clean_vec([min_x, layer, min_z]),
            "max": clean_vec([max_x, layer + height, max_z]),
            "color": color,
            "type": asset_info["type"],
        },
        asset_info["bounds_source"],
    )


def layer_cells_overlapped_by_solid(cells, cell_size, solid, padding):
    expanded = [solid["min"][0] - padding, solid["min"][2] - padding, solid["max"][0] + padding, solid["max"][2] + padding]
    overlapped = set()
    ix0 = math.floor(expanded[0] / cell_size)
    iz0 = math.floor(expanded[1] / cell_size)
    ix1 = math.ceil(expanded[2] / cell_size) - 1
    iz1 = math.ceil(expanded[3] / cell_size) - 1
    for ix in range(ix0, ix1 + 1):
        for iz in range(iz0, iz1 + 1):
            if (ix, iz) in cells:
                cell_rect = [ix * cell_size, iz * cell_size, (ix + 1) * cell_size, (iz + 1) * cell_size]
                if rects_overlap(cell_rect, expanded):
                    overlapped.add((ix, iz))
    return overlapped


def build_layered_asset_solids(
    entities,
    extracted_dir,
    static_limit,
    include_source,
    nav_areas,
    height_step,
    layer_cells,
    nav_cell_size,
    spawn_points,
    spawn_clearance,
    asset_bounds,
    allowed_types,
    color_palette,
    height_tolerance,
    height_search_distance,
    max_asset_nav_cells,
    player_radius,
):
    solids = []
    metadata = {}

    def try_add(model, origin, yaw, skipped, accepted, seen_key=None):
        layer, layer_error = asset_layer_for_origin(
            origin,
            nav_areas,
            height_step,
            height_tolerance,
            height_search_distance,
            layer_cells,
            nav_cell_size,
        )
        if layer is None:
            skipped[layer_error] += 1
            return
        solid, bounds_source = asset_solid_from_origin_layered(
            model,
            origin,
            yaw,
            layer,
            color_palette,
            extracted_dir,
            asset_bounds,
            allowed_types,
        )
        if not solid:
            skipped[bounds_source] += 1
            return
        if solid_hits_spawn_clearance(solid, spawn_points, spawn_clearance):
            skipped["spawn_clearance"] += 1
            return
        if max_asset_nav_cells:
            occupied = layer_cells_overlapped_by_solid(layer_cells.get(layer, set()), nav_cell_size, solid, player_radius)
            if len(occupied) > max_asset_nav_cells:
                skipped["nav_coverage"] += 1
                return
        if seen_key is not None and seen_key in seen:
            skipped["duplicate"] += 1
            return
        if seen_key is not None:
            seen.add(seen_key)
        solids.append(solid)
        accepted[(model, bounds_source)] += 1

    if include_source in {"entities", "both"}:
        accepted = collections.Counter()
        skipped = collections.Counter()
        prop_classes = {"prop_physics_multiplayer", "prop_dynamic", "prop_dynamic_override", "prop_physics"}
        for entity in entities:
            if entity.get("classname") not in prop_classes:
                continue
            model = entity.get("model")
            origin_text = entity.get("origin")
            if not model or not origin_text:
                skipped["missing_model_or_origin"] += 1
                continue
            try_add(model, parse_vec3(origin_text), parse_yaw(entity), skipped, accepted)
        metadata["entities"] = {
            "count": sum(accepted.values()),
            "top_models": [{"model": model, "bounds_source": source, "count": count} for (model, source), count in accepted.most_common(40)],
            "skipped": dict(skipped),
        }

    if include_source in {"static", "both"}:
        accepted = collections.Counter()
        skipped = collections.Counter()
        static_props_path = extracted_dir / "geometry" / "static_props.json"
        seen = set()
        if not static_props_path.exists():
            skipped["missing_static_props_json"] += 1
        else:
            static_props = json.loads(static_props_path.read_text(encoding="utf-8"))
            for prop in static_props.get("props", []):
                if sum(accepted.values()) >= static_limit:
                    skipped["limit_reached"] += 1
                    continue
                model = prop.get("model")
                origin = prop.get("origin")
                if not model or not origin:
                    skipped["missing_model_or_origin"] += 1
                    continue
                yaw = prop.get("angles", [0, 0, 0])[1] if len(prop.get("angles", [])) >= 2 else 0.0
                key = (model, round(origin[0] / 16), round(origin[1] / 16), round(origin[2] / 16))
                try_add(model, origin, yaw, skipped, accepted, key)
        metadata["static"] = {
            "count": sum(accepted.values()),
            "top_models": [{"model": model, "bounds_source": source, "count": count} for (model, source), count in accepted.most_common(40)],
            "skipped": dict(skipped),
        }

    return solids, metadata


def scale_vec(values, scale, yaw_index=None):
    scaled = []
    for index, value in enumerate(values):
        scaled.append(value if yaw_index is not None and index == yaw_index else value * scale)
    return clean_vec(scaled)


def offset_output_y(output, amount):
    if abs(amount) < 1e-9:
        return output
    for solid in output["solids"]:
        solid["min"][1] = clean_num(solid["min"][1] + amount)
        solid["max"][1] = clean_num(solid["max"][1] + amount)
    for ramp in output["ramps"]:
        ramp["min"][1] = clean_num(ramp["min"][1] + amount)
        ramp["max"][1] = clean_num(ramp["max"][1] + amount)
        ramp["yMin"] = clean_num(ramp["yMin"] + amount)
        ramp["yMax"] = clean_num(ramp["yMax"] + amount)
    if len(output.get("surfStart", [])) >= 2:
        output["surfStart"][1] = clean_num(output["surfStart"][1] + amount)
    return output


def scale_map_output(output, scale):
    if abs(scale - 1.0) < 1e-9:
        return output
    output["killY"] = clean_num(output["killY"] * scale)
    for solid in output["solids"]:
        solid["min"] = scale_vec(solid["min"], scale)
        solid["max"] = scale_vec(solid["max"], scale)
    for ramp in output["ramps"]:
        ramp["min"] = scale_vec(ramp["min"], scale)
        ramp["max"] = scale_vec(ramp["max"], scale)
        ramp["yMin"] = clean_num(ramp["yMin"] * scale)
        ramp["yMax"] = clean_num(ramp["yMax"] * scale)
    output["surfStart"] = scale_vec(output["surfStart"], scale, yaw_index=3)
    output["surfFinish"]["min"] = scale_vec(output["surfFinish"]["min"], scale)
    output["surfFinish"]["max"] = scale_vec(output["surfFinish"]["max"], scale)
    for team in ("t", "ct"):
        output["spawns"][team] = [
            clean_vec([spawn[0] * scale, spawn[1] * scale, *spawn[2:]])
            for spawn in output["spawns"][team]
        ]
    for site in output["bombsites"]:
        site["min"] = scale_vec(site["min"], scale)
        site["max"] = scale_vec(site["max"], scale)
    return output


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("bsp", type=Path)
    parser.add_argument("--extracted", type=Path, required=True)
    parser.add_argument("--nav", type=Path)
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--name", default="de_mirage_csgo_layered")
    parser.add_argument("--title", default="De Mirage CS:GO Layered")
    parser.add_argument("--theme", default="sand")
    parser.add_argument("--scale", type=float, default=1.0)
    parser.add_argument("--color-mode", choices=["auto", "fixed"], default="auto")
    parser.add_argument("--nav-cell-size", type=int, default=128)
    parser.add_argument("--height-step", type=float, default=64.0)
    parser.add_argument("--floor-thickness", type=float, default=24.0)
    parser.add_argument("--wall-height", type=float, default=144.0)
    parser.add_argument("--min-ground-y", type=float, default=128.0)
    parser.add_argument("--kill-y-below-ground", type=float, default=64.0)
    parser.add_argument("--spawn-height", type=float, default=48.0)
    parser.add_argument("--include-roof", action="store_true")
    parser.add_argument("--roof-height", type=float, help="Roof bottom height above each layer. Defaults to --wall-height.")
    parser.add_argument("--roof-thickness", type=float, default=32.0)
    parser.add_argument("--roof-padding", type=float, default=0.0)
    parser.add_argument("--wall-thickness", type=float, default=24.0)
    parser.add_argument("--min-wall-length", type=float, default=64.0)
    parser.add_argument("--min-wall-opening", type=float, default=ENGINE_PLAYER_RADIUS * 2.0)
    parser.add_argument("--spawn-wall-clearance", type=float, default=16.0)
    parser.add_argument("--ramp-wall-clearance", type=float, default=128.0)
    parser.add_argument("--ramp-min-rise", type=float, default=28.0)
    parser.add_argument("--ramp-max-rise", type=float, default=192.0)
    parser.add_argument("--slope-mode", choices=["auto", "stairs", "ramps"], default="auto")
    parser.add_argument("--stair-step-height", type=float, default=16.0)
    parser.add_argument("--stair-min-steps", type=int, default=2)
    parser.add_argument("--stair-max-steps", type=int, default=16)
    parser.add_argument("--stair-min-tread-depth", type=float, default=ENGINE_PLAYER_RADIUS * 2.0)
    parser.add_argument("--stair-side-walls", action="store_true")
    parser.add_argument("--player-height", type=float, default=ENGINE_PLAYER_HEIGHT)
    parser.add_argument("--player-step-height", type=float, default=ENGINE_STEP_HEIGHT)
    parser.add_argument("--no-walls", action="store_true")
    parser.add_argument("--no-foundation", action="store_true")
    parser.add_argument("--no-global-containment-walls", action="store_true")
    parser.add_argument("--no-containment-seal-walls", action="store_true")
    parser.add_argument("--containment-seal-grid-size", type=float, default=64.0)
    parser.add_argument("--containment-seal-edge-depth", type=float, default=16.0)
    parser.add_argument("--containment-seal-wall-thickness", type=float)
    parser.add_argument("--include-assets", action="store_true")
    parser.add_argument("--asset-source", choices=["entities", "static", "both"], default="both")
    parser.add_argument("--asset-density", choices=sorted(ASSET_DENSITY_LIMITS), default="medium")
    parser.add_argument("--asset-types", default="crate")
    parser.add_argument("--asset-bounds", choices=["auto", "heuristic", "mdl"], default="auto")
    parser.add_argument("--asset-height-tolerance", type=float, default=96.0)
    parser.add_argument("--asset-height-search-distance", type=float, default=128.0)
    parser.add_argument("--spawn-clearance", type=float, default=48.0)
    parser.add_argument("--player-radius", type=float, default=ENGINE_PLAYER_RADIUS)
    parser.add_argument("--max-asset-nav-cells", type=int, default=6)
    parser.add_argument("--static-asset-limit", type=int)
    parser.add_argument("--include-spawn-yaw", action="store_true")
    args = parser.parse_args()
    if args.scale <= 0:
        raise SystemExit("--scale must be greater than 0")
    if args.roof_height is not None and args.roof_height <= 0:
        raise SystemExit("--roof-height must be greater than 0")
    if args.roof_thickness <= 0:
        raise SystemExit("--roof-thickness must be greater than 0")
    if args.kill_y_below_ground < 0:
        raise SystemExit("--kill-y-below-ground must be greater than or equal to 0")
    if args.stair_step_height <= 0:
        raise SystemExit("--stair-step-height must be greater than 0")
    if args.stair_min_steps <= 0 or args.stair_max_steps <= 0:
        raise SystemExit("--stair step counts must be greater than 0")
    if args.stair_max_steps < args.stair_min_steps:
        raise SystemExit("--stair-max-steps must be greater than or equal to --stair-min-steps")
    if args.stair_min_tread_depth <= 0:
        raise SystemExit("--stair-min-tread-depth must be greater than 0")
    if args.min_wall_opening < 0:
        raise SystemExit("--min-wall-opening must be greater than or equal to 0")
    if args.player_height <= 0:
        raise SystemExit("--player-height must be greater than 0")
    if args.player_step_height <= 0:
        raise SystemExit("--player-step-height must be greater than 0")
    resolved_roof_height = args.roof_height if args.roof_height is not None else args.wall_height
    if args.include_roof and resolved_roof_height < args.player_height:
        raise SystemExit("--roof-height must provide at least --player-height of standing clearance")
    if args.containment_seal_grid_size <= 0:
        raise SystemExit("--containment-seal-grid-size must be greater than 0")
    if args.containment_seal_edge_depth <= 0:
        raise SystemExit("--containment-seal-edge-depth must be greater than 0")
    if args.containment_seal_wall_thickness is not None and args.containment_seal_wall_thickness <= 0:
        raise SystemExit("--containment-seal-wall-thickness must be greater than 0")
    allowed_asset_types = parse_asset_types(args.asset_types)
    static_asset_limit = args.static_asset_limit
    if static_asset_limit is None:
        static_asset_limit = ASSET_DENSITY_LIMITS[args.asset_density]
    color_palette, color_metadata = build_color_palette(args.bsp, args.color_mode)

    nav_path = args.nav or args.bsp.with_suffix(".nav")
    nav = parse_nav_area_rects(nav_path)
    layer_cells = collections.defaultdict(set)
    ramps = []
    stair_solids = []
    stair_side_wall_solids = []
    stair_rects = []
    slope_clearance_rects = []
    stats = collections.Counter()

    for area in nav["areas"]:
        ramp = ramp_candidate(area, args.ramp_min_rise, args.ramp_max_rise)
        if ramp:
            ramp_rect = [ramp["min"][0], ramp["min"][2], ramp["max"][0], ramp["max"][2]]
            slope_clearance_rects.append(ramp_rect)
            playable_steps = usable_stair_steps(
                ramp,
                args.stair_step_height,
                args.stair_min_steps,
                args.stair_max_steps,
                args.stair_min_tread_depth,
                args.player_step_height,
            )
            use_ramp = args.slope_mode == "ramps" or (
                args.slope_mode == "auto" and playable_steps is None
            )
            if use_ramp:
                ramps.append(ramp_to_output(ramp, args.floor_thickness, color_palette["ramp"]))
                stats["walkable_ramp_areas"] += 1
            else:
                # --slope-mode stairs is an explicit visual override. Keep its
                # old bounded segmentation even for slopes too short for a hull.
                if playable_steps is None:
                    span = abs(ramp["yMax"] - ramp["yMin"])
                    playable_steps = max(args.stair_min_steps, int(math.ceil(span / args.stair_step_height)))
                    playable_steps = min(args.stair_max_steps, playable_steps)
                new_stairs, new_stair_rects = stair_solids_for_ramp(
                    ramp,
                    args.floor_thickness,
                    playable_steps,
                    color_palette["floor"],
                )
                stair_solids.extend(new_stairs)
                stair_rects.extend(new_stair_rects)
                if not args.no_walls and args.stair_side_walls:
                    stair_side_wall_solids.extend(
                        stair_side_walls_for_ramp(ramp, args.wall_thickness, args.wall_height, color_palette["wall"])
                    )
                stats["stair_areas"] += 1
            if args.slope_mode == "auto" and playable_steps is None:
                stats["unplayable_stair_fallbacks"] += 1
            stats["ramp_areas"] += 1
            continue
        rasterize_area(area, args.nav_cell_size, args.height_step, layer_cells)
        stats["floor_areas"] += 1

    entities = load_entities(args.extracted)
    models = load_models(args.extracted)
    spawns, spawn_yaws, spawn_entities = build_spawns_layered(
        entities,
        nav["areas"],
        args.height_step,
        layer_cells,
        args.nav_cell_size,
        args.spawn_height,
    )
    spawn_points = spawn_clearance_points(spawn_entities)
    bombsites = build_bombsites(entities, models)
    ramp_rects = [[ramp["min"][0], ramp["min"][2], ramp["max"][0], ramp["max"][2]] for ramp in ramps]
    slope_clearance_rects.extend(ramp_rects)

    floor_color = color_palette["floor"]
    wall_color = color_palette["wall"]
    roof_color = color_palette["roof"]
    solids = []
    floor_rect_count = 0
    wall_rect_count = 0
    roof_rect_count = 0
    global_wall_rect_count = 0
    global_wall_min_y = None
    global_wall_max_y = None
    seal_wall_rect_count = 0
    removed_spawn_walls = 0
    removed_ramp_walls = 0
    removed_global_spawn_walls = 0
    removed_seal_spawn_walls = 0
    removed_stair_side_spawn_walls = 0
    wall_base_y, highest_walkable_y = walkable_vertical_bounds(
        layer_cells, stair_solids, ramps, args.floor_thickness
    )
    projected_cells = set()
    for cells in layer_cells.values():
        projected_cells.update(cells)
    for slope_rect in slope_clearance_rects:
        add_rect_to_cells(projected_cells, slope_rect, args.nav_cell_size)
    foundation_top_y = wall_base_y + args.floor_thickness
    foundation_rects = merge_cells_to_rects(projected_cells, args.nav_cell_size) if projected_cells else []
    wall_clearance_rects = slope_clearance_rects
    if stair_side_wall_solids:
        for solid in stair_side_wall_solids:
            solid["min"][1] = clean_num(wall_base_y)
        before = len(stair_side_wall_solids)
        stair_side_wall_solids = [
            solid for solid in stair_side_wall_solids if not solid_hits_spawn_clearance(solid, spawn_points, args.spawn_wall_clearance)
        ]
        removed_stair_side_spawn_walls = before - len(stair_side_wall_solids)
    foundation_floor_rect_count = 0
    if not args.no_foundation:
        foundation_floor_rect_count = len(foundation_rects)
        solids.extend(floor_solid(rect, foundation_top_y, args.floor_thickness, floor_color) for rect in foundation_rects)
    for layer in sorted(layer_cells):
        rects = merge_cells_to_rects(layer_cells[layer], args.nav_cell_size)
        floor_rect_count += len(rects)
        solids.extend(floor_solid(rect, layer, args.floor_thickness, floor_color) for rect in rects)
        if args.include_roof:
            roof_rects = [expand_rect(rect, args.roof_padding) for rect in rects]
            roof_rect_count += len(roof_rects)
            solids.extend(roof_solid(rect, layer, resolved_roof_height, args.roof_thickness, roof_color) for rect in roof_rects)
        if args.no_walls:
            continue
        wall_rects = boundary_wall_rects(layer_cells[layer], args.nav_cell_size, args.wall_thickness)
        wall_rects = filter_wall_rects(wall_rects, args.min_wall_length)
        before = len(wall_rects)
        wall_rects = [
            rect for rect in wall_rects if not rect_hits_spawn_clearance(rect, spawn_points, args.spawn_wall_clearance)
        ]
        removed_spawn_walls += before - len(wall_rects)
        before = len(wall_rects)
        wall_rects = [
            rect for rect in wall_rects if not wall_hits_ramp_clearance(rect, wall_clearance_rects, args.ramp_wall_clearance)
        ]
        removed_ramp_walls += before - len(wall_rects)
        wall_rects = close_narrow_wall_gaps(wall_rects, args.min_wall_opening)
        wall_rect_count += len(wall_rects)
        solids.extend(wall_solid(rect, layer, args.wall_height, wall_color, wall_base_y) for rect in wall_rects)

    solids.extend(stair_solids)
    solids.extend(stair_side_wall_solids)
    stair_roof_count = 0
    if args.include_roof and stair_solids:
        for stair in stair_solids:
            rect = [stair["min"][0], stair["min"][2], stair["max"][0], stair["max"][2]]
            solids.append(roof_solid(rect, stair["max"][1], resolved_roof_height, args.roof_thickness, roof_color))
            stair_roof_count += 1

    if not args.no_walls and not args.no_global_containment_walls and layer_cells:
        global_wall_rects = boundary_wall_rects(projected_cells, args.nav_cell_size, args.wall_thickness)
        global_wall_rects = filter_wall_rects(global_wall_rects, args.min_wall_length)
        before = len(global_wall_rects)
        global_wall_rects = [
            rect for rect in global_wall_rects if not rect_hits_spawn_clearance(rect, spawn_points, args.spawn_wall_clearance)
        ]
        removed_global_spawn_walls = before - len(global_wall_rects)
        global_wall_rects = close_narrow_wall_gaps(global_wall_rects, args.min_wall_opening)
        # A nav mesh does not encode whether a higher layer is a bridge or a
        # building. Treat emitted structural edges as solid curtains so higher
        # wall strips cannot expose the outside through lower layers.
        global_wall_min_y = wall_base_y
        wall_top_padding = args.wall_height
        if args.include_roof:
            wall_top_padding = max(wall_top_padding, resolved_roof_height + args.roof_thickness)
        global_wall_max_y = highest_walkable_y + wall_top_padding
        global_wall_rect_count = len(global_wall_rects)
        solids.extend(tall_wall_solid(rect, global_wall_min_y, global_wall_max_y, wall_color) for rect in global_wall_rects)

    if not args.no_walls and not args.no_containment_seal_walls:
        roof_max_thickness = max(48.0, args.roof_thickness)
        floor_solids = [solid for solid in solids if solid.get("type") == "floor"]
        existing_wall_solids = [
            solid for solid in solids if solid.get("type") == "wall" and not is_roof_like_solid(solid, roof_max_thickness)
        ]
        seal_wall_thickness = (
            args.containment_seal_wall_thickness
            if args.containment_seal_wall_thickness is not None
            else min(args.wall_thickness, args.containment_seal_edge_depth)
        )
        seal_wall_rects = containment_seal_wall_rects(
            floor_solids,
            existing_wall_solids,
            ramps,
            args.containment_seal_grid_size,
            args.containment_seal_edge_depth,
            seal_wall_thickness,
            args.min_wall_length,
        )
        if seal_wall_rects:
            before = len(seal_wall_rects)
            seal_wall_rects = [
                rect for rect in seal_wall_rects if not rect_hits_spawn_clearance(rect, spawn_points, args.spawn_wall_clearance)
            ]
            removed_seal_spawn_walls = before - len(seal_wall_rects)
            floor_tops = [solid["max"][1] for solid in floor_solids]
            if seal_wall_rects:
                seal_min_y = wall_base_y
                seal_max_y = max(floor_tops) + max(args.wall_height, resolved_roof_height + args.roof_thickness)
                seal_wall_rect_count = len(seal_wall_rects)
                solids.extend(tall_wall_solid(rect, seal_min_y, seal_max_y, wall_color) for rect in seal_wall_rects)

    asset_solids = []
    asset_metadata = {}
    if args.include_assets:
        asset_solids, asset_metadata = build_layered_asset_solids(
            entities,
            args.extracted,
            static_asset_limit,
            args.asset_source,
            nav["areas"],
            args.height_step,
            layer_cells,
            args.nav_cell_size,
            spawn_points,
            args.spawn_clearance,
            args.asset_bounds,
            allowed_asset_types,
            color_palette,
            args.asset_height_tolerance,
            args.asset_height_search_distance,
            args.max_asset_nav_cells,
            args.player_radius,
        )
        solids.extend(asset_solids)

    first_spawn = spawn_entities["t"][0] if spawn_entities["t"] else None
    if first_spawn:
        surf_start = clean_vec(source_to_js(parse_vec3(first_spawn["origin"]))) + [clean_num(parse_yaw(first_spawn))]
    else:
        surf_start = [0, 0, 0, 0]
    surf_finish = {"min": [0, 0], "max": [0, 0]}
    if bombsites:
        surf_finish = {"min": bombsites[0]["min"], "max": bombsites[0]["max"]}

    min_y = min([solid["min"][1] for solid in solids] + [ramp["min"][1] for ramp in ramps] + [-512])
    output = {
        "name": args.name,
        "title": args.title,
        "theme": args.theme,
        "mode": "defusal",
        "killY": clean_num(min_y - 512),
        "solids": solids,
        "ramps": ramps,
        "surfStart": surf_start,
        "surfFinish": surf_finish,
        "spawns": spawns,
        "bombsites": bombsites,
        "hostages": [],
        "rescueZone": None,
        "ladders": [],
    }
    if args.include_spawn_yaw:
        output["spawnYaws"] = spawn_yaws
    floor_tops = [solid["max"][1] for solid in solids if solid.get("type") == "floor"]
    min_floor_top = min(floor_tops) if floor_tops else 0.0
    vertical_offset = args.min_ground_y - min_floor_top
    offset_output_y(output, vertical_offset)
    output["killY"] = clean_num(args.min_ground_y - args.kill_y_below_ground)
    scale_map_output(output, args.scale)
    engine_runtime = add_engine_runtime_fields(output)

    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(output, separators=(",", ":")) + "\n", encoding="utf-8")
    metadata = {
        "source_bsp": str(args.bsp),
        "source_nav": str(nav_path),
        "source_extracted_dir": str(args.extracted),
        "coordinate_transform": "source [x,y,z] -> layered js [x,z,y]",
        "note": "Nav areas are quantized into height layers; auto slope mode uses stairs only within the game hull limits and otherwise emits walkable one-axis ramps.",
        "parameters": {
            "scale": args.scale,
            "color_mode": args.color_mode,
            "nav_cell_size": args.nav_cell_size,
            "height_step": args.height_step,
            "floor_thickness": args.floor_thickness,
            "wall_height": args.wall_height,
            "min_ground_y": args.min_ground_y,
            "vertical_offset": vertical_offset,
            "kill_y_below_ground": args.kill_y_below_ground,
            "spawn_height": args.spawn_height,
            "roof_enabled": args.include_roof,
            "roof_height": resolved_roof_height,
            "roof_thickness": args.roof_thickness,
            "roof_padding": args.roof_padding,
            "wall_thickness": args.wall_thickness,
            "min_wall_length": args.min_wall_length,
            "min_wall_opening": args.min_wall_opening,
            "spawn_wall_clearance": args.spawn_wall_clearance,
            "ramp_wall_clearance": args.ramp_wall_clearance,
            "ramp_min_rise": args.ramp_min_rise,
            "ramp_max_rise": args.ramp_max_rise,
            "slope_mode": args.slope_mode,
            "stair_step_height": args.stair_step_height,
            "stair_min_steps": args.stair_min_steps,
            "stair_max_steps": args.stair_max_steps,
            "stair_min_tread_depth": args.stair_min_tread_depth,
            "stair_side_walls": args.stair_side_walls,
            "player_height": args.player_height,
            "player_step_height": args.player_step_height,
            "walls_enabled": not args.no_walls,
            "foundation_enabled": not args.no_foundation,
            "global_containment_walls_enabled": not args.no_walls and not args.no_global_containment_walls,
            "containment_seal_walls_enabled": not args.no_walls and not args.no_containment_seal_walls,
            "containment_seal_grid_size": args.containment_seal_grid_size,
            "containment_seal_edge_depth": args.containment_seal_edge_depth,
            "containment_seal_wall_thickness": (
                args.containment_seal_wall_thickness
                if args.containment_seal_wall_thickness is not None
                else min(args.wall_thickness, args.containment_seal_edge_depth)
            ),
            "assets_enabled": args.include_assets,
            "asset_source": args.asset_source,
            "asset_density": args.asset_density,
            "asset_types": "all" if allowed_asset_types is None else sorted(allowed_asset_types),
            "asset_bounds": args.asset_bounds,
            "asset_height_tolerance": args.asset_height_tolerance,
            "asset_height_search_distance": args.asset_height_search_distance,
            "spawn_clearance": args.spawn_clearance,
            "player_radius": args.player_radius,
            "max_asset_nav_cells": args.max_asset_nav_cells,
            "static_asset_limit": static_asset_limit,
        },
        "output_counts": {
            "layers": len(layer_cells),
            "floor_rects": floor_rect_count,
            "foundation_floor_rects": foundation_floor_rect_count,
            "wall_rects": wall_rect_count,
            "global_wall_rects": global_wall_rect_count,
            "seal_wall_rects": seal_wall_rect_count,
            "roof_rects": roof_rect_count,
            "stair_solids": len(stair_solids),
            "stair_side_walls": len(stair_side_wall_solids),
            "stair_roofs": stair_roof_count,
            "asset_solids": len(asset_solids),
            "wall_rects_removed_for_spawn_clearance": removed_spawn_walls,
            "wall_rects_removed_for_ramp_clearance": removed_ramp_walls,
            "global_wall_rects_removed_for_spawn_clearance": removed_global_spawn_walls,
            "seal_wall_rects_removed_for_spawn_clearance": removed_seal_spawn_walls,
            "stair_side_walls_removed_for_spawn_clearance": removed_stair_side_spawn_walls,
            "solids": len(solids),
            "ramps": len(ramps),
            "t_spawns": len(spawns["t"]),
            "ct_spawns": len(spawns["ct"]),
            "bombsites": len(bombsites),
        },
        "spawn_yaws": spawn_yaws,
        "colors": {
            "palette": color_palette,
            "hex": {role: f"#{color:06x}" for role, color in color_palette.items()},
            "inference": color_metadata,
        },
        "assets": asset_metadata,
        "nav": {
            "path": nav["path"],
            "version": nav["version"],
            "subversion": nav["subversion"],
            "area_count": nav["area_count"],
        },
        "conversion": dict(stats),
        "global_containment": {
            "min_y": None if global_wall_min_y is None else clean_num((global_wall_min_y + vertical_offset) * args.scale),
            "max_y": None if global_wall_max_y is None else clean_num((global_wall_max_y + vertical_offset) * args.scale),
            "structural_wall_base_y": clean_num((wall_base_y + vertical_offset) * args.scale),
            "foundation_top_y": clean_num((foundation_top_y + vertical_offset) * args.scale),
        },
        "engine_runtime": engine_runtime,
    }
    args.out.with_suffix(".meta.json").write_text(json.dumps(metadata, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(metadata["output_counts"], indent=2))


if __name__ == "__main__":
    main()
