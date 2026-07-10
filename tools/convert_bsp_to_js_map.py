#!/usr/bin/env python3
"""Convert extracted Source/VBSP map geometry into the JS clone map JSON shape.

The target schema only supports axis-aligned solids and simple one-axis ramps,
so this is an approximation layer over Source's convex brush geometry.
"""

from __future__ import annotations

import argparse
import collections
import hashlib
import json
import math
from pathlib import Path

from extract_bsp_geometry import (
    Bsp,
    parse_brushes_and_sides,
    parse_planes,
    parse_texdata_and_materials,
    parse_texinfo,
)
from convert_bsp_to_js_map_flat import add_engine_runtime_fields, source_to_combat_spawn


SKIP_TOOL_MATERIALS = {
    "TOOLS/TOOLSSKYBOX",
    "TOOLS/TOOLSTRIGGER",
    "TOOLS/TOOLSAREAPORTAL",
    "TOOLS/TOOLSHINT",
    "TOOLS/TOOLSSKIP",
    "TOOLS/TOOLSBLACK",
    "TOOLS/TOOLSINVISIBLE",
}

INVISIBLE_SOLID_MATERIALS = {"TOOLS/TOOLSCLIP"}


def clean_num(value: float):
    if abs(value) < 1e-5:
        value = 0.0
    rounded = round(value)
    if abs(value - rounded) < 1e-4:
        return int(rounded)
    return round(value, 3)


def clean_vec(values):
    return [clean_num(float(value)) for value in values]


def source_to_js(point):
    # Source is X/Y horizontal and Z vertical. The JS clone schema appears to use
    # X/Z horizontal and Y vertical.
    return [point[0], point[2], point[1]]


def source_bounds_to_js(mins, maxs):
    converted = [source_to_js(mins), source_to_js(maxs)]
    return [
        [min(converted[0][i], converted[1][i]) for i in range(3)],
        [max(converted[0][i], converted[1][i]) for i in range(3)],
    ]


def source_bounds_to_horizontal_rect(mins, maxs):
    return [[mins[0], mins[1]], [maxs[0], maxs[1]]]


def parse_vec3(value: str):
    return [float(part) for part in value.split()[:3]]


def parse_yaw(entity):
    angles = entity.get("angles")
    if not angles:
        return 0.0
    parts = angles.split()
    if len(parts) >= 2:
        return float(parts[1])
    return 0.0


def color_for_key(key: str | None) -> int:
    digest = hashlib.sha1((key or "default").encode("utf-8", errors="replace")).digest()
    r = 48 + digest[0] % 160
    g = 48 + digest[1] % 160
    b = 48 + digest[2] % 160
    return (r << 16) | (g << 8) | b


def choose_material(materials):
    visible = [mat for mat in materials if mat and not mat.startswith("TOOLS/")]
    if visible:
        return sorted(visible)[0]
    if any(mat in INVISIBLE_SOLID_MATERIALS for mat in materials):
        return "TOOLS/TOOLSCLIP"
    return sorted(materials)[0] if materials else "__unknown__"


def should_skip_brush(materials):
    material_set = {mat for mat in materials if mat}
    if not material_set:
        return True
    if material_set & INVISIBLE_SOLID_MATERIALS:
        return False
    if all(mat in SKIP_TOOL_MATERIALS or mat == "TOOLS/TOOLSNODRAW" for mat in material_set):
        return True
    if all(mat.startswith("TOOLS/") for mat in material_set):
        return True
    return False


def classify_solid_type(js_mins, js_maxs, material):
    dx = js_maxs[0] - js_mins[0]
    dy = js_maxs[1] - js_mins[1]
    dz = js_maxs[2] - js_mins[2]
    if dy <= 24 and max(dx, dz) >= dy * 4:
        return "floor"
    footprint_min = min(dx, dz)
    footprint_max = max(dx, dz)
    if dy >= 64 and footprint_min <= 48 and footprint_max >= 128:
        return "wall"
    if dy >= 64 and footprint_min <= 96 and footprint_max <= 192:
        return "crate"
    if material == "TOOLS/TOOLSCLIP" and dy >= 64:
        return "wall"
    return "crate"


def dot(a, b):
    return a[0] * b[0] + a[1] * b[1] + a[2] * b[2]


def cross(a, b):
    return [
        a[1] * b[2] - a[2] * b[1],
        a[2] * b[0] - a[0] * b[2],
        a[0] * b[1] - a[1] * b[0],
    ]


def add(a, b):
    return [a[0] + b[0], a[1] + b[1], a[2] + b[2]]


def mul(a, scale):
    return [a[0] * scale, a[1] * scale, a[2] * scale]


def plane_intersection(p1, p2, p3):
    n1, d1 = p1
    n2, d2 = p2
    n3, d3 = p3
    denom = dot(n1, cross(n2, n3))
    if abs(denom) < 1e-6:
        return None
    numerator = add(add(mul(cross(n2, n3), d1), mul(cross(n3, n1), d2)), mul(cross(n1, n2), d3))
    return mul(numerator, 1.0 / denom)


def brush_planes(brush):
    sides = [side for side in brush["sides"] if not side.get("bevel")]
    if len(sides) < 4:
        sides = brush["sides"]
    planes = []
    seen = set()
    for side in sides:
        plane = side.get("plane")
        if not plane:
            continue
        normal = [float(v) for v in plane["normal"]]
        dist = float(plane["dist"])
        key = tuple(round(v, 5) for v in normal + [dist])
        if key in seen:
            continue
        seen.add(key)
        planes.append((normal, dist, side.get("material")))
    return planes


def brush_vertices(brush):
    planes = brush_planes(brush)
    points = []
    for i in range(len(planes)):
        for j in range(i + 1, len(planes)):
            for k in range(j + 1, len(planes)):
                point = plane_intersection((planes[i][0], planes[i][1]), (planes[j][0], planes[j][1]), (planes[k][0], planes[k][1]))
                if point is None:
                    continue
                if all(dot(normal, point) <= dist + 0.05 for normal, dist, _ in planes):
                    if not any(sum((point[n] - other[n]) ** 2 for n in range(3)) < 0.01 for other in points):
                        points.append(point)
    return points


def bounds(points):
    return (
        [min(point[i] for point in points) for i in range(3)],
        [max(point[i] for point in points) for i in range(3)],
    )


def is_degenerate(mins, maxs):
    return any(maxs[i] - mins[i] < 0.5 for i in range(3))


def ramp_candidate(brush, source_mins, source_maxs):
    candidates = []
    for normal, dist, material in brush_planes(brush):
        nz = normal[2]
        horizontal = math.hypot(normal[0], normal[1])
        if horizontal > 0.05 and 0.05 < abs(nz) < 0.98:
            candidates.append((normal, dist, material))
    if not candidates:
        return None
    # Prefer visible sloped faces, then the most upward/downward sloped face.
    candidates.sort(key=lambda item: (0 if item[2] and not item[2].startswith("TOOLS/") else 1, -abs(item[0][2])))
    normal, dist, material = candidates[0]
    axis_source = 0 if abs(normal[0]) >= abs(normal[1]) else 1
    axis_js = 0 if axis_source == 0 else 2
    other_source = 1 if axis_source == 0 else 0
    other_mid = (source_mins[other_source] + source_maxs[other_source]) * 0.5

    def height_at(axis_value):
        point = [0.0, 0.0]
        point[axis_source] = axis_value
        point[other_source] = other_mid
        if abs(normal[2]) < 1e-6:
            return None
        return (dist - normal[0] * point[0] - normal[1] * point[1]) / normal[2]

    h_min = height_at(source_mins[axis_source])
    h_max = height_at(source_maxs[axis_source])
    if h_min is None or h_max is None:
        return None
    h_min = max(source_mins[2], min(source_maxs[2], h_min))
    h_max = max(source_mins[2], min(source_maxs[2], h_max))
    if abs(h_max - h_min) < 1:
        return None
    return {
        "axis": axis_js,
        "yMin": h_min,
        "yMax": h_max,
        "material": material,
    }


def load_entities(extracted_dir: Path):
    return json.loads((extracted_dir / "entities.json").read_text(encoding="utf-8"))["entities"]


def load_models(extracted_dir: Path):
    return {model["id"]: model for model in json.loads((extracted_dir / "geometry" / "models.json").read_text(encoding="utf-8"))}


def build_spawns(entities):
    result = {"t": [], "ct": []}
    spawn_yaws = {"t": [], "ct": []}
    spawn_entities = {"t": [], "ct": []}
    for entity in entities:
        classname = entity.get("classname")
        if classname == "info_player_terrorist" and "origin" in entity:
            spawn_entities["t"].append(entity)
        elif classname == "info_player_counterterrorist" and "origin" in entity:
            spawn_entities["ct"].append(entity)
    for team in ("t", "ct"):
        for entity in spawn_entities[team]:
            yaw = clean_num(parse_yaw(entity))
            result[team].append(source_to_combat_spawn(parse_vec3(entity["origin"]), yaw))
            spawn_yaws[team].append(yaw)
    return result, spawn_yaws, spawn_entities


def build_bombsites(entities, models):
    sites = []
    for entity in entities:
        if entity.get("classname") != "func_bomb_target":
            continue
        model_ref = entity.get("model", "")
        if not model_ref.startswith("*") or not model_ref[1:].isdigit():
            continue
        model = models.get(int(model_ref[1:]))
        if not model:
            continue
        targetname = (entity.get("targetname") or "").upper()
        explode_target = (entity.get("BombExplode") or "").lower()
        if "A" in targetname or "fire_a" in explode_target:
            name = "A"
        elif "B" in targetname or "fire_b" in explode_target:
            name = "B"
        else:
            name = str(len(sites) + 1)
        rect_min, rect_max = source_bounds_to_horizontal_rect(model["mins"], model["maxs"])
        if entity.get("origin"):
            origin = parse_vec3(entity["origin"])
            rect_min = [rect_min[0] + origin[0], rect_min[1] + origin[1]]
            rect_max = [rect_max[0] + origin[0], rect_max[1] + origin[1]]
        sites.append({"name": name, "min": clean_vec(rect_min), "max": clean_vec(rect_max)})
    sites.sort(key=lambda item: {"A": 0, "B": 1}.get(item["name"], 2))
    named_sites = [site for site in sites if site["name"] in {"A", "B"}]
    if {site["name"] for site in named_sites} == {"A", "B"}:
        return named_sites
    return sites[:2]


def build_ladders(entities):
    ladders = []
    for entity in entities:
        if entity.get("classname") != "info_ladder":
            continue
        try:
            source_mins = [float(entity[f"mins.{axis}"]) for axis in ("x", "y", "z")]
            source_maxs = [float(entity[f"maxs.{axis}"]) for axis in ("x", "y", "z")]
        except KeyError:
            continue
        js_mins, js_maxs = source_bounds_to_js(source_mins, source_maxs)
        dx = source_maxs[0] - source_mins[0]
        dy = source_maxs[1] - source_mins[1]
        normal = [1, 0] if dx <= dy else [0, 1]
        ladders.append({"min": clean_vec(js_mins), "max": clean_vec(js_maxs), "n": normal})
    return ladders


def convert_brushes(bsp_path: Path):
    bsp = Bsp(bsp_path)
    planes = parse_planes(bsp)
    texdata, _ = parse_texdata_and_materials(bsp)
    texinfo = parse_texinfo(bsp)
    brushes, _ = parse_brushes_and_sides(bsp, planes, texinfo, texdata)

    solids = []
    ramps = []
    stats = collections.Counter()
    material_counts = collections.Counter()
    for brush in brushes:
        materials = {side.get("material") for side in brush["sides"] if side.get("material")}
        if should_skip_brush(materials):
            stats["skipped_material"] += 1
            continue
        points = brush_vertices(brush)
        if len(points) < 4:
            stats["skipped_no_vertices"] += 1
            continue
        source_mins, source_maxs = bounds(points)
        if is_degenerate(source_mins, source_maxs):
            stats["skipped_degenerate"] += 1
            continue
        material = choose_material(materials)
        material_counts[material] += 1
        js_mins, js_maxs = source_bounds_to_js(source_mins, source_maxs)
        ramp = ramp_candidate(brush, source_mins, source_maxs)
        if ramp:
            ramps.append(
                {
                    "min": clean_vec(js_mins),
                    "max": clean_vec(js_maxs),
                    "axis": ramp["axis"],
                    "yMin": clean_num(ramp["yMin"]),
                    "yMax": clean_num(ramp["yMax"]),
                    "rot": 0,
                    "walk": True,
                    "color": color_for_key(ramp.get("material") or material),
                }
            )
            stats["ramps"] += 1
            continue
        solids.append(
            {
                "min": clean_vec(js_mins),
                "max": clean_vec(js_maxs),
                "color": color_for_key(material),
                "type": classify_solid_type(js_mins, js_maxs, material),
            }
        )
        stats["solids"] += 1

    return solids, ramps, {
        "brush_count": len(brushes),
        "converted": dict(stats),
        "top_materials": [{"material": mat, "count": count} for mat, count in material_counts.most_common(40)],
    }


def scale_vec(values, scale, yaw_index=None):
    scaled = []
    for index, value in enumerate(values):
        scaled.append(value if yaw_index is not None and index == yaw_index else value * scale)
    return clean_vec(scaled)


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
    for ladder in output["ladders"]:
        ladder["min"] = scale_vec(ladder["min"], scale)
        ladder["max"] = scale_vec(ladder["max"], scale)
    return output


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("bsp", type=Path)
    parser.add_argument("--extracted", type=Path, required=True)
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--name", default="de_mirage_csgo")
    parser.add_argument("--title", default="De Mirage CS:GO")
    parser.add_argument("--theme", default="sand")
    parser.add_argument("--scale", type=float, default=1.0)
    parser.add_argument("--include-spawn-yaw", action="store_true")
    args = parser.parse_args()
    if args.scale <= 0:
        raise SystemExit("--scale must be greater than 0")

    entities = load_entities(args.extracted)
    models = load_models(args.extracted)
    spawns, spawn_yaws, spawn_entities = build_spawns(entities)
    bombsites = build_bombsites(entities, models)
    ladders = build_ladders(entities)
    solids, ramps, conversion = convert_brushes(args.bsp)
    all_vertical_mins = [solid["min"][1] for solid in solids] + [ramp["min"][1] for ramp in ramps]
    kill_y = clean_num((min(all_vertical_mins) if all_vertical_mins else -512) - 512)
    first_spawn = spawn_entities["t"][0] if spawn_entities["t"] else None
    if first_spawn:
        surf_start = clean_vec(source_to_js(parse_vec3(first_spawn["origin"]))) + [clean_num(parse_yaw(first_spawn))]
    else:
        surf_start = [0, 0, 0, 0]
    finish_rect = {"min": [0, 0], "max": [0, 0]}
    if bombsites:
        finish_rect = {"min": bombsites[0]["min"], "max": bombsites[0]["max"]}

    output = {
        "name": args.name,
        "title": args.title,
        "theme": args.theme,
        "mode": "defusal",
        "killY": kill_y,
        "solids": solids,
        "ramps": ramps,
        "surfStart": surf_start,
        "surfFinish": finish_rect,
        "spawns": spawns,
        "bombsites": bombsites,
        "hostages": [],
        "rescueZone": None,
        "ladders": ladders,
    }
    if args.include_spawn_yaw:
        output["spawnYaws"] = spawn_yaws
    scale_map_output(output, args.scale)
    engine_runtime = add_engine_runtime_fields(output)

    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(output, separators=(",", ":")) + "\n", encoding="utf-8")
    metadata = {
        "source_bsp": str(args.bsp),
        "source_extracted_dir": str(args.extracted),
        "coordinate_transform": "source [x,y,z] -> js [x,z,y]",
        "note": "Convex BSP brushes were approximated as axis-aligned boxes or one-axis ramps. Props were intentionally omitted.",
        "parameters": {
            "scale": args.scale,
            "include_spawn_yaw": args.include_spawn_yaw,
        },
        "output_counts": {
            "solids": len(solids),
            "ramps": len(ramps),
            "t_spawns": len(spawns["t"]),
            "ct_spawns": len(spawns["ct"]),
            "bombsites": len(bombsites),
            "ladders": len(ladders),
        },
        "spawn_yaws": spawn_yaws,
        "brush_conversion": conversion,
        "engine_runtime": engine_runtime,
    }
    args.out.with_suffix(".meta.json").write_text(json.dumps(metadata, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(metadata["output_counts"], indent=2))


if __name__ == "__main__":
    main()
