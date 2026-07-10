#!/usr/bin/env python3
"""Create a minimal flat JS-clone map from a Source/VBSP map.

This converter intentionally throws away vertical gameplay detail. It projects
walkable-looking floor faces onto one flat plane, projects large wall/clip faces
into coarse blockers, grid-merges both sets, and emits the simple JS map schema.
Ramps and ladders are omitted. Props are omitted unless --include-assets is set,
in which case matching prop models are approximated as box solids.
"""

from __future__ import annotations

import argparse
from bisect import bisect_right
import collections
import hashlib
import json
import math
from pathlib import Path
import struct

from extract_bsp_geometry import (
    Bsp,
    face_vertex_indices,
    material_for_texinfo,
    parse_edges,
    parse_faces,
    parse_planes,
    parse_surfedges,
    parse_texdata_and_materials,
    parse_texinfo,
    parse_vertices,
)


SKIP_MATERIAL_PREFIXES = ("TOOLS/TOOLSSKYBOX", "TOOLS/TOOLSAREAPORTAL", "TOOLS/TOOLSTRIGGER")
SKIP_MATERIALS = {
    "TOOLS/TOOLSNODRAW",
    "TOOLS/TOOLSSKIP",
    "TOOLS/TOOLSHINT",
    "TOOLS/TOOLSBLACK",
    "TOOLS/TOOLSINVISIBLE",
}
BLOCKER_TOOL_MATERIALS = {"TOOLS/TOOLSCLIP"}

ASSET_RULES = [
    (("trashdumpster",), (128, 96, 96), "crate"),
    (("bus",), (260, 96, 96), "crate"),
    (("cara_", "hatchback", "van.mdl"), (190, 96, 72), "crate"),
    (("pallet_cinderblock",), (112, 96, 72), "crate"),
    (("pallet",), (96, 72, 40), "crate"),
    (("crate", "food_crates"), (72, 72, 56), "crate"),
    (("cinderblock", "stoneblock"), (56, 40, 32), "crate"),
    (("trash_can", "trashcan", "trashbin"), (48, 48, 72), "crate"),
    (("garbage128", "garbage256", "trashcluster"), (96, 72, 48), "crate"),
    (("bomb_tanks",), (128, 80, 72), "crate"),
    (("bomb_shells",), (96, 64, 56), "crate"),
    (("shelving", "bookcase", "dresser", "refrigerator", "chiller"), (112, 64, 112), "crate"),
    (("sofa", "couch"), (128, 72, 64), "crate"),
    (("table",), (80, 64, 48), "crate"),
    (("chair", "barstool", "stool"), (48, 48, 64), "crate"),
    (("bench",), (112, 48, 48), "crate"),
    (("cash_register", "cashregister"), (40, 32, 24), "crate"),
    (("electrical_box", "powerbox", "switchbox"), (64, 32, 80), "crate"),
    (("bucket", "paintcan", "water_jug"), (36, 36, 48), "crate"),
    (("pot", "potted_plant", "plant"), (56, 56, 80), "crate"),
    (("tv.mdl",), (64, 32, 48), "crate"),
]

ASSET_DENSITY_LIMITS = {
    "low": 60,
    "medium": 120,
    "high": 240,
    "full": 1000000,
}

ASSET_SKIP_KEYWORDS = (
    "skybox",
    "cloud",
    "window",
    "awning",
    "roof",
    "lamp",
    "lantern",
    "light",
    "wire",
    "cable",
    "curb",
    "fence",
    "railing",
    "telephone",
    "antenna",
    "dish",
    "pipe",
    "rug",
    "pillow",
    "cloth",
    "tarp",
    "shutter",
    "wall_hole",
    "door",
    "arch",
    "vent",
    "plaster",
    "buildingedge",
    "props_foliage",
    "foliage",
    "bush",
    "grass",
    "balcony_planter",
    "broom",
    "chimney",
    "foliage/tree",
    "palm",
    "hangingvines",
)


def clean_num(value: float):
    if abs(value) < 1e-5:
        value = 0.0
    rounded = round(value)
    if abs(value - rounded) < 1e-4:
        return int(rounded)
    return round(value, 3)


def clean_vec(values):
    return [clean_num(float(value)) for value in values]


def parse_vec3(value: str):
    return [float(part) for part in value.split()[:3]]


def parse_yaw(entity):
    parts = entity.get("angles", "0 0 0").split()
    return float(parts[1]) if len(parts) >= 2 else 0.0


def color_for_key(key: str) -> int:
    digest = hashlib.sha1(key.encode("utf-8", errors="replace")).digest()
    r = 48 + digest[0] % 160
    g = 48 + digest[1] % 160
    b = 48 + digest[2] % 160
    return (r << 16) | (g << 8) | b


def rgb_to_int(rgb) -> int:
    r, g, b = (max(0, min(255, int(round(value)))) for value in rgb)
    return (r << 16) | (g << 8) | b


def int_to_rgb(value: int):
    return ((value >> 16) & 255, (value >> 8) & 255, value & 255)


def mix_rgb(a, b, amount):
    amount = max(0.0, min(1.0, amount))
    return tuple(a[index] * (1.0 - amount) + b[index] * amount for index in range(3))


FALLBACK_ROLE_COLORS = {
    "floor": 0x9C9A88,
    "wall": 0xB28C5E,
    "roof": 0x6F675C,
    "crate": 0x8D6039,
    "pillar": 0x7D8278,
    "ramp": 0x9A8066,
}

ENGINE_THEME_LOOKS = {
    "sand": {
        "skyTop": 0x5590C8,
        "skyHorizon": 0xECD9AC,
        "skyColor": 0xA2D2EA,
        "fogColor": 0xDCCCA6,
        "fogNear": 1900,
        "fogFar": 4800,
        "sunDir": [-0.35, -1.0, 0.3],
        "sunColor": 0xFFF2D0,
        "sunIntensity": 1.5,
        "hemiSky": 0xD2E8F6,
        "hemiGround": 0x8F7A4C,
        "hemiIntensity": 1.0,
    },
    "ice": {
        "skyTop": 0x2A3038,
        "skyHorizon": 0x8794A0,
        "skyColor": 0x6B7883,
        "fogColor": 0x8794A0,
        "fogNear": 3500,
        "fogFar": 12000,
        "sunDir": [0.2, -1.0, 0.3],
        "sunColor": 0xF4F0E8,
        "sunIntensity": 1.1,
        "hemiSky": 0xC8D4E0,
        "hemiGround": 0x40484F,
        "hemiIntensity": 1.0,
    },
    "industrial": {
        "skyTop": 0x1F3A63,
        "skyHorizon": 0x9FC4E0,
        "skyColor": 0x5A8FC0,
        "fogColor": 0x8FB4D4,
        "fogNear": 4200,
        "fogFar": 15000,
        "sunDir": [0.35, -1.0, 0.15],
        "sunColor": 0xFFFFFF,
        "sunIntensity": 1.25,
        "hemiSky": 0xBFE0F4,
        "hemiGround": 0x35506A,
        "hemiIntensity": 1.05,
    },
}


def engine_theme(theme: str) -> str:
    normalized = (theme or "").strip().lower()
    if normalized in ENGINE_THEME_LOOKS:
        return normalized
    return "sand" if normalized in {"dust", "desert"} else "ice"


def buyzone_for_spawns(spawns, padding=300.0):
    if not spawns:
        return {"min": [-300, -300], "max": [300, 300]}
    xs = [spawn[0] for spawn in spawns]
    # Combat spawns use the game schema [x, z, yaw].
    zs = [spawn[1] for spawn in spawns]
    return {
        "min": clean_vec([min(xs) - padding, min(zs) - padding]),
        "max": clean_vec([max(xs) + padding, max(zs) + padding]),
    }


def add_engine_runtime_fields(output, buyzone_padding=300.0):
    """Add the fields required when a converted JSON map is loaded by the game."""
    points = []
    for solid in output.get("solids", []):
        points.extend((solid["min"], solid["max"]))
    for ramp in output.get("ramps", []):
        points.extend((ramp["min"], ramp["max"]))
    if points:
        mins = [min(point[index] for point in points) for index in range(3)]
        maxs = [max(point[index] for point in points) for index in range(3)]
    else:
        mins, maxs = [-1000, -100, -1000], [1000, 400, 1000]

    theme = engine_theme(output.get("theme", ""))
    output["theme"] = theme
    output.update(ENGINE_THEME_LOOKS[theme])
    output["bounds"] = {
        "min": clean_vec([mins[0] - 1000, min(mins[1], output["killY"]) - 200, mins[2] - 1000]),
        "max": clean_vec([maxs[0] + 1000, maxs[1] + 800, maxs[2] + 1000]),
    }
    output["buyzones"] = {
        "t": buyzone_for_spawns(output.get("spawns", {}).get("t", []), buyzone_padding),
        "ct": buyzone_for_spawns(output.get("spawns", {}).get("ct", []), buyzone_padding),
    }
    # The server owns graph construction; the empty field satisfies the runtime schema.
    output["nav"] = []
    return {
        "theme": theme,
        "buyzone_padding": buyzone_padding,
        "bounds": output["bounds"],
        "nav_source": "server_generated",
    }

FIXED_ROLE_KEYS = {
    "floor": "flat_floor",
    "wall": "flat_blocker",
    "roof": "flat_roof",
    "crate": "flat_asset_entity",
    "pillar": "flat_asset_static",
    "ramp": "layered_ramp",
}

MATERIAL_COLOR_HINTS = [
    (("wood", "plywood", "pallet", "beam"), 0x8C5E35),
    (("metal", "corrugated", "rail", "trim", "door"), 0x747B7A),
    (("tile", "floor", "stone", "step", "counter"), 0xA6A394),
    (("brick",), 0x9D6A4C),
    (("plaster", "stucco", "wall"), 0xBE9A69),
    (("dust", "sand", "mirage", "ground", "base"), 0xB99563),
    (("concrete", "cement", "blacktop"), 0x85857D),
    (("blue",), 0x6B879D),
    (("salmon", "red"), 0xB07764),
]


def material_hint_rgb(material: str | None):
    if not material:
        return None
    lowered = material.lower()
    for keywords, color in MATERIAL_COLOR_HINTS:
        if any(keyword in lowered for keyword in keywords):
            return int_to_rgb(color)
    return None


def reflectivity_rgb(reflectivity):
    if not reflectivity or sum(max(0.0, float(value)) for value in reflectivity[:3]) < 0.03:
        return None
    rgb = []
    for value in reflectivity[:3]:
        linear = max(0.0, min(1.0, float(value)))
        rgb.append(255 * (linear ** (1.0 / 2.2)))
    return tuple(rgb)


def color_for_material(material: str | None, reflectivity=None, role: str = "wall") -> int:
    rgb = reflectivity_rgb(reflectivity)
    hint = material_hint_rgb(material)
    if rgb and hint:
        rgb = mix_rgb(rgb, hint, 0.45)
    elif hint:
        rgb = hint
    elif rgb is None:
        rgb = int_to_rgb(color_for_key(material or role))

    if role == "floor":
        rgb = mix_rgb(rgb, (178, 172, 150), 0.16)
    elif role == "wall":
        rgb = mix_rgb(rgb, (178, 140, 88), 0.12)
    elif role == "roof":
        rgb = mix_rgb(rgb, (80, 76, 70), 0.30)
    return rgb_to_int(rgb)


def face_texdata(face, texinfo, texdata):
    texinfo_id = face.get("texinfo", -1)
    if texinfo_id < 0 or texinfo_id >= len(texinfo):
        return None
    texdata_id = texinfo[texinfo_id]["texdata"]
    if texdata_id < 0 or texdata_id >= len(texdata):
        return None
    return texdata[texdata_id]


def weighted_average_rgb(samples, fallback: int) -> int:
    total_weight = sum(weight for _rgb, weight in samples)
    if total_weight <= 0:
        return fallback
    rgb = [0.0, 0.0, 0.0]
    for sample_rgb, weight in samples:
        for index in range(3):
            rgb[index] += sample_rgb[index] * weight
    return rgb_to_int(value / total_weight for value in rgb)


def infer_bsp_role_colors(bsp_path: Path):
    """Infer a small role palette from BSP material reflectivity and names."""
    samples = collections.defaultdict(list)
    material_samples = collections.defaultdict(collections.Counter)
    try:
        bsp = Bsp(bsp_path)
        planes = parse_planes(bsp)
        texdata, _ = parse_texdata_and_materials(bsp)
        texinfo = parse_texinfo(bsp)
        faces = parse_faces(bsp, 7)
    except Exception as exc:
        return dict(FALLBACK_ROLE_COLORS), {"mode": "auto", "error": str(exc)}

    for face in faces:
        texdata_item = face_texdata(face, texinfo, texdata)
        material = texdata_item.get("material") if texdata_item else material_for_texinfo(face["texinfo"], texinfo, texdata)
        if material_is_skipped(material):
            continue
        normal = face_normal(face, planes)
        if normal[2] > 0.72:
            role = "floor"
        elif normal[2] < -0.72:
            role = "roof"
        elif abs(normal[2]) < 0.35:
            role = "wall"
        else:
            continue
        color = color_for_material(material, (texdata_item or {}).get("reflectivity"), role)
        weight = max(1, int(face.get("numedges", 1)))
        samples[role].append((int_to_rgb(color), weight))
        material_samples[role][material or "__unknown__"] += weight

    palette = dict(FALLBACK_ROLE_COLORS)
    for role in ("floor", "wall", "roof"):
        palette[role] = weighted_average_rgb(samples[role], palette[role])
    if not samples["roof"]:
        palette["roof"] = rgb_to_int(mix_rgb(int_to_rgb(palette["wall"]), (72, 70, 66), 0.45))

    metadata = {
        "mode": "auto",
        "samples": {role: len(samples[role]) for role in ("floor", "wall", "roof")},
        "top_materials": {
            role: [{"material": material, "weight": weight} for material, weight in material_samples[role].most_common(8)]
            for role in ("floor", "wall", "roof")
        },
    }
    return palette, metadata


def build_color_palette(bsp_path: Path, mode: str):
    if mode == "fixed":
        palette = {role: color_for_key(key) for role, key in FIXED_ROLE_KEYS.items()}
        return palette, {"mode": "fixed"}
    palette, metadata = infer_bsp_role_colors(bsp_path)
    for role, color in FALLBACK_ROLE_COLORS.items():
        palette.setdefault(role, color)
    return palette, metadata


def parse_asset_types(value: str):
    if value.strip().lower() == "all":
        return None
    allowed = {part.strip().lower() for part in value.split(",") if part.strip()}
    valid = {"crate", "wall"}
    unknown = allowed - valid
    if unknown:
        raise argparse.ArgumentTypeError(f"unknown asset type(s): {', '.join(sorted(unknown))}")
    return allowed


def asset_model_is_skipped(model: str | None):
    if not model:
        return True
    lowered = model.lower()
    return any(keyword in lowered for keyword in ASSET_SKIP_KEYWORDS)


def asset_info_for_model(model: str | None):
    if not model:
        return None
    if asset_model_is_skipped(model):
        return None
    lowered = model.lower()
    for keywords, dims, solid_type in ASSET_RULES:
        if any(keyword in lowered for keyword in keywords):
            return {"dims": dims, "type": solid_type, "bounds_source": "heuristic"}
    return None


def classify_asset_type_from_dims(dims):
    return "crate"


def model_path_candidates(extracted_dir: Path, model: str):
    normalized = model.replace("\\", "/").lstrip("/")
    return [
        extracted_dir / "pakfile_files" / normalized,
        extracted_dir / "pakfile_text" / normalized,
    ]


def parse_mdl_bounds(mdl_path: Path):
    try:
        data = mdl_path.read_bytes()
    except OSError:
        return None
    if len(data) < 152 or data[:4] not in {b"IDST", b"IDAG"}:
        return None
    try:
        hull_min = struct.unpack_from("<3f", data, 104)
        hull_max = struct.unpack_from("<3f", data, 116)
    except struct.error:
        return None
    dims = [hull_max[i] - hull_min[i] for i in range(3)]
    if any(dim <= 0 or dim > 4096 for dim in dims):
        return None
    # Source model bounds are X/Y horizontal and Z vertical.
    return (dims[0], dims[1], dims[2])


def model_bounds_for_asset(extracted_dir: Path, model: str):
    for candidate in model_path_candidates(extracted_dir, model):
        if candidate.exists() and candidate.suffix.lower() == ".mdl":
            dims = parse_mdl_bounds(candidate)
            if dims:
                return dims
    return None


def asset_info_for_model_with_bounds(model: str | None, extracted_dir: Path, asset_bounds: str):
    if asset_model_is_skipped(model):
        return None
    info = asset_info_for_model(model)
    if not model:
        return None
    mdl_dims = model_bounds_for_asset(extracted_dir, model) if asset_bounds in {"auto", "mdl"} else None
    if mdl_dims:
        if not info and asset_bounds != "mdl":
            return None
        solid_type = info["type"] if info else classify_asset_type_from_dims(mdl_dims)
        return {"dims": mdl_dims, "type": solid_type, "bounds_source": "mdl"}
    if asset_bounds == "mdl":
        return None
    return info


def material_is_skipped(material: str | None) -> bool:
    if not material:
        return True
    if material in SKIP_MATERIALS:
        return True
    return any(material.startswith(prefix) for prefix in SKIP_MATERIAL_PREFIXES)


def material_is_blocker(material: str | None) -> bool:
    if not material:
        return False
    if material in BLOCKER_TOOL_MATERIALS:
        return True
    if material.startswith("TOOLS/"):
        return False
    return not material_is_skipped(material)


def source_to_flat_js(point, ground_y: float):
    # Source: X/Y horizontal, Z vertical. Flat JS: X/Z horizontal, fixed Y.
    return [point[0], ground_y, point[1]]


def source_to_combat_spawn(point, yaw):
    """Convert Source [x, y, z] to the game's combat spawn [x, z, yaw]."""
    return clean_vec([point[0], point[1], yaw])


def source_rect_to_js_solid(rect, min_y, max_y, color, solid_type):
    min_x, min_z, max_x, max_z = rect
    return {
        "min": clean_vec([min_x, min_y, min_z]),
        "max": clean_vec([max_x, max_y, max_z]),
        "color": color,
        "type": solid_type,
    }


def expand_rect(rect, amount):
    min_x, min_z, max_x, max_z = rect
    return [min_x - amount, min_z - amount, max_x + amount, max_z + amount]


def source_bounds_to_horizontal_rect(mins, maxs):
    return [[mins[0], mins[1]], [maxs[0], maxs[1]]]


def polygon_area_xy(points):
    if len(points) < 3:
        return 0.0
    area = 0.0
    for i, point in enumerate(points):
        nxt = points[(i + 1) % len(points)]
        area += point[0] * nxt[1] - nxt[0] * point[1]
    return abs(area) * 0.5


def face_normal(face, planes):
    plane = planes[face["planenum"]]
    normal = list(plane["normal"])
    if face["side"]:
        normal = [-normal[0], -normal[1], -normal[2]]
    return normal


def rect_from_points_xy(points, expand=0.0):
    min_x = min(point[0] for point in points) - expand
    min_y = min(point[1] for point in points) - expand
    max_x = max(point[0] for point in points) + expand
    max_y = max(point[1] for point in points) + expand
    return [min_x, min_y, max_x, max_y]


def add_rect_to_cells(cells, rect, cell_size, bounds_filter=None):
    min_x, min_y, max_x, max_y = rect
    if max_x <= min_x or max_y <= min_y:
        return 0
    ix0 = math.floor(min_x / cell_size)
    iy0 = math.floor(min_y / cell_size)
    ix1 = math.ceil(max_x / cell_size) - 1
    iy1 = math.ceil(max_y / cell_size) - 1
    added = 0
    for ix in range(ix0, ix1 + 1):
        for iy in range(iy0, iy1 + 1):
            if bounds_filter and not bounds_filter(ix, iy):
                continue
            if (ix, iy) not in cells:
                added += 1
            cells.add((ix, iy))
    return added


def cells_from_rects(rects, cell_size):
    cells = set()
    for rect in rects:
        add_rect_to_cells(cells, rect, cell_size)
    return cells


def point_in_cells(x, z, cells, cell_size):
    if not cells:
        return True
    ix = math.floor(x / cell_size)
    iz = math.floor(z / cell_size)
    if (ix, iz) in cells:
        return True
    # Allow a one-cell tolerance for objects placed against nav boundaries.
    for dx in (-1, 0, 1):
        for dz in (-1, 0, 1):
            if (ix + dx, iz + dz) in cells:
                return True
    return False


def merge_cells_to_rects(cells, cell_size):
    remaining = set(cells)
    rects = []
    while remaining:
        ix, iy = min(remaining, key=lambda item: (item[1], item[0]))
        width = 1
        while (ix + width, iy) in remaining:
            width += 1
        height = 1
        while all((ix + dx, iy + height) in remaining for dx in range(width)):
            height += 1
        for dx in range(width):
            for dy in range(height):
                remaining.remove((ix + dx, iy + dy))
        rects.append([ix * cell_size, iy * cell_size, (ix + width) * cell_size, (iy + height) * cell_size])
    return rects


def snap_rect(rect, snap_size):
    min_x, min_z, max_x, max_z = rect
    return [
        math.floor(min_x / snap_size) * snap_size,
        math.floor(min_z / snap_size) * snap_size,
        math.ceil(max_x / snap_size) * snap_size,
        math.ceil(max_z / snap_size) * snap_size,
    ]


def rects_to_variable_cells(rects):
    clean_rects = [rect for rect in rects if rect[2] > rect[0] and rect[3] > rect[1]]
    if not clean_rects:
        return [], [], set()
    xs = sorted({rect[0] for rect in clean_rects} | {rect[2] for rect in clean_rects})
    zs = sorted({rect[1] for rect in clean_rects} | {rect[3] for rect in clean_rects})
    x_index = {value: index for index, value in enumerate(xs)}
    z_index = {value: index for index, value in enumerate(zs)}
    cells = set()
    for rect in clean_rects:
        for ix in range(x_index[rect[0]], x_index[rect[2]]):
            for iz in range(z_index[rect[1]], z_index[rect[3]]):
                cells.add((ix, iz))
    return xs, zs, cells


def merge_variable_cells_to_rects(xs, zs, cells):
    remaining = set(cells)
    rects = []
    while remaining:
        ix, iz = min(remaining, key=lambda item: (zs[item[1]], xs[item[0]]))
        width = 1
        while (ix + width, iz) in remaining:
            width += 1
        height = 1
        while all((ix + dx, iz + height) in remaining for dx in range(width)):
            height += 1
        for dx in range(width):
            for dz in range(height):
                remaining.remove((ix + dx, iz + dz))
        rects.append([xs[ix], zs[iz], xs[ix + width], zs[iz + height]])
    return rects


def merge_edge_segments(segments):
    groups = collections.defaultdict(list)
    for line, start, end, side in segments:
        groups[(line, side)].append((start, end))
    merged = []
    for (line, side), spans in groups.items():
        spans = sorted(spans)
        start, end = spans[0]
        for next_start, next_end in spans[1:] + [(None, None)]:
            if next_start is not None and abs(next_start - end) < 1e-4:
                end = next_end
                continue
            merged.append((line, start, end, side))
            start, end = next_start, next_end
    return merged


def boundary_wall_rects_variable(xs, zs, cells, thickness):
    horizontal_segments = []
    vertical_segments = []
    for ix, iz in cells:
        x0, x1 = xs[ix], xs[ix + 1]
        z0, z1 = zs[iz], zs[iz + 1]
        if (ix, iz - 1) not in cells:
            horizontal_segments.append((z0, x0, x1, "north"))
        if (ix, iz + 1) not in cells:
            horizontal_segments.append((z1, x0, x1, "south"))
        if (ix - 1, iz) not in cells:
            vertical_segments.append((x0, z0, z1, "west"))
        if (ix + 1, iz) not in cells:
            vertical_segments.append((x1, z0, z1, "east"))

    rects = []
    for z, x0, x1, side in merge_edge_segments(horizontal_segments):
        if side == "north":
            rects.append([x0, z - thickness, x1, z])
        else:
            rects.append([x0, z, x1, z + thickness])
    for x, z0, z1, side in merge_edge_segments(vertical_segments):
        if side == "west":
            rects.append([x - thickness, z0, x, z1])
        else:
            rects.append([x, z0, x + thickness, z1])
    return rects


def filter_wall_rects(rects, min_length):
    if min_length <= 0:
        return rects
    filtered = []
    for rect in rects:
        length = max(rect[2] - rect[0], rect[3] - rect[1])
        if length >= min_length:
            filtered.append(rect)
    return filtered


def point_in_rects(x, z, rects, tolerance=0.0):
    for min_x, min_z, max_x, max_z in rects:
        if min_x - tolerance <= x <= max_x + tolerance and min_z - tolerance <= z <= max_z + tolerance:
            return True
    return False


def make_uniform_nav_graph(cells, cell_size):
    return {"kind": "uniform", "cells": set(cells), "cell_size": cell_size}


def make_variable_nav_graph(xs, zs, cells):
    return {"kind": "variable", "cells": set(cells), "xs": xs, "zs": zs}


def nav_cell_rect(graph, cell):
    ix, iz = cell
    if graph["kind"] == "uniform":
        cell_size = graph["cell_size"]
        return [ix * cell_size, iz * cell_size, (ix + 1) * cell_size, (iz + 1) * cell_size]
    return [graph["xs"][ix], graph["zs"][iz], graph["xs"][ix + 1], graph["zs"][iz + 1]]


def nav_cell_center(graph, cell):
    rect = nav_cell_rect(graph, cell)
    return ((rect[0] + rect[2]) * 0.5, (rect[1] + rect[3]) * 0.5)


def nav_cell_for_point(graph, x, z):
    cells = graph["cells"]
    if not cells:
        return None
    if graph["kind"] == "uniform":
        cell_size = graph["cell_size"]
        direct = (math.floor(x / cell_size), math.floor(z / cell_size))
        if direct in cells:
            return direct
        candidates = []
        for dx in (-1, 0, 1):
            for dz in (-1, 0, 1):
                candidate = (direct[0] + dx, direct[1] + dz)
                if candidate in cells:
                    cx, cz = nav_cell_center(graph, candidate)
                    candidates.append(((cx - x) ** 2 + (cz - z) ** 2, candidate))
        return min(candidates)[1] if candidates else None

    xs = graph["xs"]
    zs = graph["zs"]
    ix = max(0, min(len(xs) - 2, bisect_right(xs, x) - 1))
    iz = max(0, min(len(zs) - 2, bisect_right(zs, z) - 1))
    direct = (ix, iz)
    if direct in cells:
        return direct
    candidates = []
    for dx in (-1, 0, 1):
        for dz in (-1, 0, 1):
            candidate = (ix + dx, iz + dz)
            if candidate in cells:
                cx, cz = nav_cell_center(graph, candidate)
                candidates.append(((cx - x) ** 2 + (cz - z) ** 2, candidate))
    return min(candidates)[1] if candidates else None


def nav_neighbors(graph, cell):
    ix, iz = cell
    cells = graph["cells"]
    for candidate in ((ix - 1, iz), (ix + 1, iz), (ix, iz - 1), (ix, iz + 1)):
        if candidate in cells:
            yield candidate


def expanded_solid_rect_xz(solid, padding):
    return [
        solid["min"][0] - padding,
        solid["min"][2] - padding,
        solid["max"][0] + padding,
        solid["max"][2] + padding,
    ]


def rects_overlap(a, b):
    return a[0] < b[2] and a[2] > b[0] and a[1] < b[3] and a[3] > b[1]


def nav_cells_overlapped_by_solid(graph, solid, padding):
    expanded = expanded_solid_rect_xz(solid, padding)
    overlapped = set()
    for cell in graph["cells"]:
        if rects_overlap(nav_cell_rect(graph, cell), expanded):
            overlapped.add(cell)
    return overlapped


def connected_protected_cells(graph, blocked_cells, protected_cells):
    goals = [cell for cell in protected_cells if cell in graph["cells"]]
    if len(goals) <= 1:
        return True
    blocked = set(blocked_cells)
    if any(cell in blocked for cell in goals):
        return False
    remaining_goals = set(goals)
    start = goals[0]
    queue = collections.deque([start])
    seen = {start}
    while queue:
        cell = queue.popleft()
        remaining_goals.discard(cell)
        if not remaining_goals:
            return True
        for neighbor in nav_neighbors(graph, cell):
            if neighbor in blocked or neighbor in seen:
                continue
            seen.add(neighbor)
            queue.append(neighbor)
    return False


def bombsite_centers(bombsites):
    centers = []
    for site in bombsites:
        centers.append(((site["min"][0] + site["max"][0]) * 0.5, (site["min"][1] + site["max"][1]) * 0.5))
    return centers


def rects_bounds(rects):
    if not rects:
        return None
    return [
        min(rect[0] for rect in rects),
        min(rect[1] for rect in rects),
        max(rect[2] for rect in rects),
        max(rect[3] for rect in rects),
    ]


def sample_rect_cells(rects, grid_size):
    """Return grid cells whose centers lie on an emitted floor rectangle."""
    bounds = rects_bounds(rects)
    if not bounds or grid_size <= 0:
        return set()
    min_x, min_z, max_x, max_z = bounds
    cells = set()
    for ix in range(math.floor(min_x / grid_size), math.ceil(max_x / grid_size)):
        for iz in range(math.floor(min_z / grid_size), math.ceil(max_z / grid_size)):
            if point_in_rects((ix + 0.5) * grid_size, (iz + 0.5) * grid_size, rects):
                cells.add((ix, iz))
    return cells


def containment_edge_probe(ix, iz, dx, dz, grid_size, depth):
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


def containment_edge_wall_rect(ix, iz, dx, dz, grid_size, thickness):
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


def edge_coverage(edge_rect, side, span_start, span_end, blockers):
    spans = []
    horizontal = side in {"north", "south"}
    for blocker in blockers:
        if not rects_overlap(edge_rect, blocker):
            continue
        if horizontal:
            start, end = max(span_start, blocker[0]), min(span_end, blocker[2])
        else:
            start, end = max(span_start, blocker[1]), min(span_end, blocker[3])
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
    return sum(end - start for start, end in merged) / max(span_end - span_start, 1e-6)


def merge_wall_rects(rects):
    groups = collections.defaultdict(list)
    for rect in rects:
        width = rect[2] - rect[0]
        depth = rect[3] - rect[1]
        if width >= depth:
            groups[("horizontal", rect[1], rect[3])].append((rect[0], rect[2]))
        else:
            groups[("vertical", rect[0], rect[2])].append((rect[1], rect[3]))

    merged = []
    for (axis, fixed_min, fixed_max), spans in groups.items():
        spans.sort()
        start, end = spans[0]
        for next_start, next_end in spans[1:] + [(None, None)]:
            if next_start is not None and next_start <= end + 1e-4:
                end = max(end, next_end)
                continue
            if axis == "horizontal":
                merged.append([start, fixed_min, end, fixed_max])
            else:
                merged.append([fixed_min, start, fixed_max, end])
            start, end = next_start, next_end
    return merged


def containment_seal_wall_rects(floor_rects, existing_wall_rects, grid_size, edge_depth, wall_thickness, min_wall_length):
    """Seal partial cells introduced when the nav and validation grids differ."""
    floor_cells = sample_rect_cells(floor_rects, grid_size)
    if not floor_cells:
        return []
    seal_rects = []
    for ix, iz in sorted(floor_cells):
        for dx, dz in ((-1, 0), (1, 0), (0, -1), (0, 1)):
            if (ix + dx, iz + dz) in floor_cells:
                continue
            probe, side, span_start, span_end = containment_edge_probe(ix, iz, dx, dz, grid_size, edge_depth)
            if edge_coverage(probe, side, span_start, span_end, existing_wall_rects) < 0.85:
                seal_rects.append(containment_edge_wall_rect(ix, iz, dx, dz, grid_size, wall_thickness))
    return filter_wall_rects(merge_wall_rects(seal_rects), min_wall_length)


def perimeter_wall_rects(bounds_rect, padding, thickness):
    min_x, min_z, max_x, max_z = bounds_rect
    outer_min_x = min_x - padding - thickness
    outer_max_x = max_x + padding + thickness
    outer_min_z = min_z - padding - thickness
    outer_max_z = max_z + padding + thickness
    inner_min_x = min_x - padding
    inner_max_x = max_x + padding
    inner_min_z = min_z - padding
    inner_max_z = max_z + padding
    return [
        [outer_min_x, outer_min_z, inner_min_x, outer_max_z],
        [inner_max_x, outer_min_z, outer_max_x, outer_max_z],
        [inner_min_x, outer_min_z, inner_max_x, inner_min_z],
        [inner_min_x, inner_max_z, inner_max_x, outer_max_z],
    ]


def boundary_wall_rects(cells, cell_size, thickness):
    north_edges = []
    south_edges = []
    west_edges = []
    east_edges = []
    for ix, iz in cells:
        x0 = ix * cell_size
        x1 = (ix + 1) * cell_size
        z0 = iz * cell_size
        z1 = (iz + 1) * cell_size
        if (ix, iz - 1) not in cells:
            north_edges.append((ix, iz, x0, z0 - thickness, x1, z0))
        if (ix, iz + 1) not in cells:
            south_edges.append((ix, iz, x0, z1, x1, z1 + thickness))
        if (ix - 1, iz) not in cells:
            west_edges.append((ix, iz, x0 - thickness, z0, x0, z1))
        if (ix + 1, iz) not in cells:
            east_edges.append((ix, iz, x1, z0, x1 + thickness, z1))

    rects = []
    for edges in (north_edges, south_edges):
        groups = collections.defaultdict(list)
        for ix, iz, x0, z0, x1, z1 in edges:
            groups[(iz, z0, z1)].append(ix)
        for (_iz, z0, z1), xs in groups.items():
            xs = sorted(xs)
            start = previous = xs[0]
            for x in xs[1:] + [None]:
                if x == previous + 1:
                    previous = x
                    continue
                rects.append([start * cell_size, z0, (previous + 1) * cell_size, z1])
                start = previous = x

    for edges in (west_edges, east_edges):
        groups = collections.defaultdict(list)
        for ix, iz, x0, z0, x1, z1 in edges:
            groups[(ix, x0, x1)].append(iz)
        for (_ix, x0, x1), zs in groups.items():
            zs = sorted(zs)
            start = previous = zs[0]
            for z in zs[1:] + [None]:
                if z == previous + 1:
                    previous = z
                    continue
                rects.append([x0, start * cell_size, x1, (previous + 1) * cell_size])
                start = previous = z
    return rects


def parse_nav_area_rects(nav_path: Path):
    """Parse Source nav areas into flat rects. Supports nav versions 9 and 16.

    The per-area layout changed across the format's history. The version gates
    below are verified against real version-9 (CS:S de_dust2) and version-16
    (CS:GO de_mirage) nav files: v9 uses 16-bit area attributes, still stores
    approach areas, and omits the light-intensity and visibility blocks that v16
    adds. Intermediate versions (10-15) are gated per the documented format
    history but are not exercised by the maps in this repo.
    """
    data = nav_path.read_bytes()
    if len(data) < 24:
        raise ValueError(f"{nav_path} is too small to be a Source nav file")
    magic, version = struct.unpack_from("<II", data, 0)
    if magic != 0xFEEDFACE:
        raise ValueError(f"{nav_path} has unexpected magic 0x{magic:08x}")
    if version not in (9, 16):
        raise ValueError(
            f"{nav_path} uses nav version {version}; this parser supports versions 9 and 16"
        )
    cursor = 8
    subversion = 0
    if version >= 10:
        subversion = struct.unpack_from("<I", data, cursor)[0]
        cursor += 4
    bsp_size = struct.unpack_from("<I", data, cursor)[0]
    cursor += 4
    if version >= 14:
        cursor += 1  # m_isAnalyzed
    place_count = struct.unpack_from("<H", data, cursor)[0]
    cursor += 2
    for _ in range(place_count):
        name_len = struct.unpack_from("<H", data, cursor)[0]
        cursor += 2 + name_len
    if version >= 12:
        cursor += 1  # has-unnamed-areas flag (present in v16, absent in v9)
    area_count = struct.unpack_from("<I", data, cursor)[0]
    cursor += 4
    areas = []
    for _ in range(area_count):
        area_start = cursor
        area_id = struct.unpack_from("<I", data, cursor)[0]
        cursor += 4
        # Attribute flags widened over the format's life: u8, then u16, then u32.
        if version <= 8:
            attributes = data[cursor]
            cursor += 1
        elif version < 13:
            attributes = struct.unpack_from("<H", data, cursor)[0]
            cursor += 2
        else:
            attributes = struct.unpack_from("<I", data, cursor)[0]
            cursor += 4
        nw = struct.unpack_from("<3f", data, cursor)
        cursor += 12
        se = struct.unpack_from("<3f", data, cursor)
        cursor += 12
        ne_z, sw_z = struct.unpack_from("<ff", data, cursor)
        cursor += 8
        for _direction in range(4):
            connection_count = struct.unpack_from("<I", data, cursor)[0]
            cursor += 4 + connection_count * 4
        hiding_spot_count = data[cursor]
        cursor += 1 + hiding_spot_count * 17
        if version < 15:
            approach_count = data[cursor]
            cursor += 1 + approach_count * 14  # here/prev/next ids + 2 traverse bytes
        encounter_count = struct.unpack_from("<I", data, cursor)[0]
        cursor += 4
        for _encounter in range(encounter_count):
            cursor += 4 + 1 + 4 + 1  # from id + dir, to id + dir
            spot_count = data[cursor]
            cursor += 1 + spot_count * 5  # each spot: u32 area id + u8 parametric distance
        cursor += 2  # place id
        for _ladder_direction in range(2):
            ladder_count = struct.unpack_from("<I", data, cursor)[0]
            cursor += 4 + ladder_count * 4
        if version >= 7:
            cursor += 8  # earliest occupy time per team
        if version >= 11:
            cursor += 16  # light intensity at the 4 corners
        if version >= 16:
            visible_area_count = struct.unpack_from("<I", data, cursor)[0]
            cursor += 4 + visible_area_count * 5
            cursor += 4  # inherit-visibility-from area id
            cursor += 1  # trailing flag
        if cursor > len(data):
            raise ValueError(f"nav area {area_id} at {area_start} overran file")
        min_x = min(nw[0], se[0])
        max_x = max(nw[0], se[0])
        min_z = min(nw[1], se[1])
        max_z = max(nw[1], se[1])
        areas.append(
            {
                "id": area_id,
                "attributes": attributes,
                "rect": [min_x, min_z, max_x, max_z],
                "nw": list(nw),
                "se": list(se),
                "ne_z": ne_z,
                "sw_z": sw_z,
            }
        )
    return {
        "path": str(nav_path),
        "version": version,
        "subversion": subversion,
        "stored_bsp_size": bsp_size,
        "area_count": area_count,
        "areas": areas,
        "bytes_remaining_after_areas": len(data) - cursor,
    }


def nav_area_height_at(area, x, z):
    # Source nav areas store nwCorner as the (min x, min y) corner and seCorner
    # as the (max x, max y) corner, so tx/tz below run west->east and
    # north->south and the four corner heights line up as labelled.
    min_x, min_z, max_x, max_z = area["rect"]
    width = max(max_x - min_x, 1e-6)
    depth = max(max_z - min_z, 1e-6)
    tx = max(0.0, min(1.0, (x - min_x) / width))
    tz = max(0.0, min(1.0, (z - min_z) / depth))
    h_nw = area["nw"][2]
    h_ne = area["ne_z"]
    h_se = area["se"][2]
    h_sw = area["sw_z"]
    north = h_nw * (1.0 - tx) + h_ne * tx
    south = h_sw * (1.0 - tx) + h_se * tx
    return north * (1.0 - tz) + south * tz


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


def nav_height_near_point(nav_areas, x, z, search_distance):
    containing = [
        area
        for area in nav_areas
        if area["rect"][0] <= x <= area["rect"][2] and area["rect"][1] <= z <= area["rect"][3]
    ]
    if containing:
        return [nav_area_height_at(area, x, z) for area in containing]
    if search_distance < 0:
        return []
    max_dist_sq = search_distance * search_distance
    nearest = []
    best_dist = None
    for area in nav_areas:
        dist_sq = rect_distance_sq_to_point(area["rect"], x, z)
        if dist_sq > max_dist_sq:
            continue
        if best_dist is None or dist_sq < best_dist - 1e-6:
            best_dist = dist_sq
            nearest = [area]
        elif abs(dist_sq - best_dist) < 1e-6:
            nearest.append(area)
    return [nav_area_height_at(area, x, z) for area in nearest]


def build_asset_height_filter(nav_projection, tolerance, search_distance):
    if tolerance < 0 or not nav_projection:
        return lambda origin: (True, None)
    nav_areas = nav_projection.get("areas", [])
    if not nav_areas:
        return lambda origin: (True, None)

    def accepts(origin):
        heights = nav_height_near_point(nav_areas, origin[0], origin[1], search_distance)
        if not heights:
            return True, None
        best_delta = min((origin[2] - height for height in heights), key=abs)
        if abs(best_delta) > tolerance:
            return False, clean_num(best_delta)
        return True, None

    return accepts


def load_entities(extracted_dir: Path):
    return json.loads((extracted_dir / "entities.json").read_text(encoding="utf-8"))["entities"]


def load_models(extracted_dir: Path):
    return {model["id"]: model for model in json.loads((extracted_dir / "geometry" / "models.json").read_text(encoding="utf-8"))}


def build_spawns(entities, ground_y, spawn_height):
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


def solid_hits_spawn_clearance(solid, spawn_points, clearance):
    if clearance <= 0:
        return False
    min_x, max_x = solid["min"][0], solid["max"][0]
    min_z, max_z = solid["min"][2], solid["max"][2]
    for x, z in spawn_points:
        if min_x <= x + clearance and max_x >= x - clearance and min_z <= z + clearance and max_z >= z - clearance:
            return True
    return False


def rect_hits_spawn_clearance(rect, spawn_points, clearance):
    if clearance <= 0:
        return False
    min_x, min_z, max_x, max_z = rect
    for x, z in spawn_points:
        if min_x <= x + clearance and max_x >= x - clearance and min_z <= z + clearance and max_z >= z - clearance:
            return True
    return False


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
    # The supplied game's defusal mode exposes two bombsite slots. Prefer the
    # source-authored A/B targets and ignore auxiliary/C trigger volumes.
    if {site["name"] for site in named_sites} == {"A", "B"}:
        return named_sites
    return sites[:2]


def rotated_footprint_size(size_x, size_z, yaw_degrees):
    radians = math.radians(yaw_degrees)
    cos_yaw = abs(math.cos(radians))
    sin_yaw = abs(math.sin(radians))
    return (
        size_x * cos_yaw + size_z * sin_yaw,
        size_x * sin_yaw + size_z * cos_yaw,
    )


def asset_solid_from_origin(model, origin, yaw, ground_y, color_palette, extracted_dir, asset_bounds, allowed_types):
    asset_info = asset_info_for_model_with_bounds(model, extracted_dir, asset_bounds)
    if not asset_info:
        return None, "unmatched_model"
    if allowed_types is not None and asset_info["type"] not in allowed_types:
        return None, "filtered_type"
    dims = asset_info["dims"]
    size_x, size_z, height = dims
    size_x, size_z = rotated_footprint_size(size_x, size_z, yaw)
    source_x, source_z = origin[0], origin[1]
    rect = [
        source_x - size_x * 0.5,
        source_z - size_z * 0.5,
        source_x + size_x * 0.5,
        source_z + size_z * 0.5,
    ]
    color = color_palette.get(asset_info["type"], color_for_key(f"flat_asset_{asset_info['type']}"))
    solid = source_rect_to_js_solid(rect, ground_y, ground_y + height, color, asset_info["type"])
    return solid, asset_info["bounds_source"]


def build_entity_asset_solids(
    entities,
    point_on_floor,
    ground_y,
    spawn_points,
    spawn_clearance,
    extracted_dir,
    asset_bounds,
    allowed_types,
    nav_graph,
    blocked_nav_cells,
    protected_nav_cells,
    player_radius,
    protect_nav_connectivity,
    max_asset_nav_cells,
    color_palette,
    asset_height_filter,
):
    solids = []
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
        origin = parse_vec3(origin_text)
        if not point_on_floor(origin[0], origin[1]):
            skipped["outside_nav"] += 1
            continue
        height_ok, _height_delta = asset_height_filter(origin)
        if not height_ok:
            skipped["height_mismatch"] += 1
            continue
        solid, bounds_source = asset_solid_from_origin(
            model,
            origin,
            parse_yaw(entity),
            ground_y,
            color_palette,
            extracted_dir,
            asset_bounds,
            allowed_types,
        )
        if not solid:
            skipped[bounds_source] += 1
            continue
        if solid_hits_spawn_clearance(solid, spawn_points, spawn_clearance):
            skipped["spawn_clearance"] += 1
            continue
        if protect_nav_connectivity:
            candidate_blocked = nav_cells_overlapped_by_solid(nav_graph, solid, player_radius)
            if max_asset_nav_cells and len(candidate_blocked) > max_asset_nav_cells:
                skipped["nav_coverage"] += 1
                continue
            if not connected_protected_cells(nav_graph, blocked_nav_cells | candidate_blocked, protected_nav_cells):
                skipped["nav_connectivity"] += 1
                continue
            blocked_nav_cells.update(candidate_blocked)
        solids.append(solid)
        accepted[(model, bounds_source)] += 1
    return solids, {
        "count": len(solids),
        "top_models": [{"model": model, "bounds_source": source, "count": count} for (model, source), count in accepted.most_common(40)],
        "skipped": dict(skipped),
    }


def build_static_asset_solids(
    extracted_dir,
    point_on_floor,
    ground_y,
    limit,
    spawn_points,
    spawn_clearance,
    asset_bounds,
    allowed_types,
    nav_graph,
    blocked_nav_cells,
    protected_nav_cells,
    player_radius,
    protect_nav_connectivity,
    max_asset_nav_cells,
    color_palette,
    asset_height_filter,
):
    static_props_path = extracted_dir / "geometry" / "static_props.json"
    if not static_props_path.exists():
        return [], {"count": 0, "top_models": {}, "skipped": {"missing_static_props_json": 1}}
    static_props = json.loads(static_props_path.read_text(encoding="utf-8"))
    solids = []
    accepted = collections.Counter()
    skipped = collections.Counter()
    seen = set()
    for prop in static_props.get("props", []):
        if len(solids) >= limit:
            skipped["limit_reached"] += 1
            continue
        model = prop.get("model")
        origin = prop.get("origin")
        if not model or not origin:
            skipped["missing_model_or_origin"] += 1
            continue
        if not point_on_floor(origin[0], origin[1]):
            skipped["outside_nav"] += 1
            continue
        height_ok, _height_delta = asset_height_filter(origin)
        if not height_ok:
            skipped["height_mismatch"] += 1
            continue
        yaw = prop.get("angles", [0, 0, 0])[1] if len(prop.get("angles", [])) >= 2 else 0.0
        solid, bounds_source = asset_solid_from_origin(
            model,
            origin,
            yaw,
            ground_y,
            color_palette,
            extracted_dir,
            asset_bounds,
            allowed_types,
        )
        if not solid:
            skipped[bounds_source] += 1
            continue
        if solid_hits_spawn_clearance(solid, spawn_points, spawn_clearance):
            skipped["spawn_clearance"] += 1
            continue
        if protect_nav_connectivity:
            candidate_blocked = nav_cells_overlapped_by_solid(nav_graph, solid, player_radius)
            if max_asset_nav_cells and len(candidate_blocked) > max_asset_nav_cells:
                skipped["nav_coverage"] += 1
                continue
            if not connected_protected_cells(nav_graph, blocked_nav_cells | candidate_blocked, protected_nav_cells):
                skipped["nav_connectivity"] += 1
                continue
        key = (model, round(origin[0] / 16), round(origin[1] / 16))
        if key in seen:
            skipped["duplicate"] += 1
            continue
        seen.add(key)
        if protect_nav_connectivity:
            blocked_nav_cells.update(candidate_blocked)
        solids.append(solid)
        accepted[(model, bounds_source)] += 1
    return solids, {
        "count": len(solids),
        "top_models": [{"model": model, "bounds_source": source, "count": count} for (model, source), count in accepted.most_common(40)],
        "skipped": dict(skipped),
    }


def extract_face_projection_cells(
    bsp_path: Path,
    floor_cell_size: int,
    blocker_cell_size: int,
    min_floor_area: float,
    min_wall_area: float,
    min_wall_height: float,
    wall_expand: float,
):
    bsp = Bsp(bsp_path)
    planes = parse_planes(bsp)
    vertices = parse_vertices(bsp)
    edges = parse_edges(bsp)
    surfedges = parse_surfedges(bsp)
    texdata, _ = parse_texdata_and_materials(bsp)
    texinfo = parse_texinfo(bsp)
    faces = parse_faces(bsp, 7)

    floor_cells = set()
    blocker_cells = set()
    material_counts = collections.Counter()
    stats = collections.Counter()

    for face in faces:
        material = material_for_texinfo(face["texinfo"], texinfo, texdata)
        if material_is_skipped(material) and material not in BLOCKER_TOOL_MATERIALS:
            stats["skipped_material"] += 1
            continue
        indices = face_vertex_indices(face, edges, surfedges)
        points = [vertices[index] for index in indices if 0 <= index < len(vertices)]
        if len(points) < 3:
            stats["skipped_bad_face"] += 1
            continue

        normal = face_normal(face, planes)
        horizontal_area = polygon_area_xy(points)
        z_span = max(point[2] for point in points) - min(point[2] for point in points)
        material_counts[material or "__unknown__"] += 1

        if normal[2] > 0.82 and horizontal_area >= min_floor_area:
            rect = rect_from_points_xy(points)
            add_rect_to_cells(floor_cells, rect, floor_cell_size)
            stats["floor_faces"] += 1
            continue

        if abs(normal[2]) < 0.35 and z_span >= min_wall_height and horizontal_area >= min_wall_area:
            if material_is_blocker(material):
                rect = rect_from_points_xy(points, wall_expand)
                add_rect_to_cells(blocker_cells, rect, blocker_cell_size)
                stats["blocker_faces"] += 1

    return floor_cells, blocker_cells, {
        "faces": len(faces),
        "stats": dict(stats),
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
    parser.add_argument("--nav", type=Path)
    parser.add_argument("--flat-source", choices=["auto", "nav", "faces"], default="auto")
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--name", default="de_mirage_csgo_flat")
    parser.add_argument("--title", default="De Mirage CS:GO Flat")
    parser.add_argument("--theme", default="sand")
    parser.add_argument("--scale", type=float, default=1.0)
    parser.add_argument("--color-mode", choices=["auto", "fixed"], default="auto")
    parser.add_argument("--ground-y", type=float, default=128.0)
    parser.add_argument("--floor-thickness", type=float, default=32.0)
    parser.add_argument("--blocker-height", type=float, default=160.0)
    parser.add_argument("--kill-y-below-ground", type=float, default=64.0)
    parser.add_argument("--include-roof", action="store_true")
    parser.add_argument("--roof-height", type=float, help="Roof bottom height above ground_y. Defaults to --blocker-height.")
    parser.add_argument("--roof-thickness", type=float, default=32.0)
    parser.add_argument("--roof-padding", type=float, default=0.0)
    parser.add_argument("--spawn-height", type=float, default=48.0)
    parser.add_argument("--floor-cell-size", type=int, default=192)
    parser.add_argument("--blocker-cell-size", type=int, default=128)
    parser.add_argument("--nav-cell-size", type=int, default=128)
    parser.add_argument("--nav-floor-mode", choices=["grid", "snap", "exact"], default="grid")
    parser.add_argument("--nav-snap-size", type=int, default=64)
    parser.add_argument("--boundary-wall-thickness", type=float, default=32.0)
    parser.add_argument("--min-wall-length", type=float, default=64.0)
    parser.add_argument("--no-containment-seal-walls", action="store_true")
    parser.add_argument("--containment-seal-grid-size", type=float, default=64.0)
    parser.add_argument("--containment-seal-edge-depth", type=float, default=16.0)
    parser.add_argument("--containment-seal-wall-thickness", type=float)
    parser.add_argument("--include-assets", action="store_true")
    parser.add_argument("--asset-source", choices=["entities", "static", "both"], default="both")
    parser.add_argument("--asset-density", choices=sorted(ASSET_DENSITY_LIMITS), default="medium")
    parser.add_argument("--asset-types", default="crate")
    parser.add_argument("--asset-bounds", choices=["auto", "heuristic", "mdl"], default="auto")
    parser.add_argument("--asset-floor-tolerance", type=float, default=64.0)
    parser.add_argument("--asset-height-tolerance", type=float, default=96.0, help="Maximum Source Z distance from nav floor height for accepted assets. Use -1 to disable.")
    parser.add_argument("--asset-height-search-distance", type=float, default=128.0)
    parser.add_argument("--static-asset-limit", type=int)
    parser.add_argument("--spawn-clearance", type=float, default=48.0)
    parser.add_argument("--spawn-wall-clearance", type=float, default=16.0)
    parser.add_argument("--player-radius", type=float, default=32.0)
    parser.add_argument("--max-asset-nav-cells", type=int, default=6)
    parser.add_argument("--no-protect-nav-connectivity", action="store_true")
    parser.add_argument("--include-spawn-yaw", action="store_true")
    parser.add_argument("--min-floor-area", type=float, default=2048.0)
    parser.add_argument("--min-wall-area", type=float, default=256.0)
    parser.add_argument("--min-wall-height", type=float, default=80.0)
    parser.add_argument("--wall-expand", type=float, default=24.0)
    parser.add_argument("--perimeter-padding", type=float, default=192.0)
    parser.add_argument("--perimeter-thickness", type=float, default=192.0)
    parser.add_argument("--no-perimeter-walls", action="store_true")
    parser.add_argument("--no-blockers", action="store_true")
    args = parser.parse_args()
    if args.scale <= 0:
        raise SystemExit("--scale must be greater than 0")
    if args.roof_height is not None and args.roof_height <= 0:
        raise SystemExit("--roof-height must be greater than 0")
    if args.roof_thickness <= 0:
        raise SystemExit("--roof-thickness must be greater than 0")
    if args.kill_y_below_ground < 0:
        raise SystemExit("--kill-y-below-ground must be greater than or equal to 0")
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
    use_nav = args.flat_source == "nav" or (args.flat_source == "auto" and nav_path.exists())
    nav_projection = None
    if use_nav:
        nav_projection = parse_nav_area_rects(nav_path)
        nav_rects = [area["rect"] for area in nav_projection["areas"]]
        nav_floor_mode = args.nav_floor_mode
        if nav_floor_mode == "grid":
            floor_cells = cells_from_rects(nav_rects, args.nav_cell_size)
            nav_graph = make_uniform_nav_graph(floor_cells, args.nav_cell_size)
            floor_rects = merge_cells_to_rects(floor_cells, args.nav_cell_size)
            blocker_rects = [] if args.no_blockers else boundary_wall_rects(
                floor_cells,
                args.nav_cell_size,
                args.boundary_wall_thickness,
            )

            def point_on_floor(x, z):
                return point_in_cells(x, z, floor_cells, args.nav_cell_size)

            nav_union_stats = {
                "mode": "grid",
                "cell_size": args.nav_cell_size,
                "cells": len(floor_cells),
            }
        else:
            source_rects = [snap_rect(rect, args.nav_snap_size) for rect in nav_rects] if nav_floor_mode == "snap" else nav_rects
            xs, zs, floor_cells = rects_to_variable_cells(source_rects)
            nav_graph = make_variable_nav_graph(xs, zs, floor_cells)
            floor_rects = merge_variable_cells_to_rects(xs, zs, floor_cells)
            blocker_rects = [] if args.no_blockers else boundary_wall_rects_variable(
                xs,
                zs,
                floor_cells,
                args.boundary_wall_thickness,
            )

            def point_on_floor(x, z):
                return point_in_rects(x, z, floor_rects, args.asset_floor_tolerance)

            nav_union_stats = {
                "mode": nav_floor_mode,
                "snap_size": args.nav_snap_size if nav_floor_mode == "snap" else None,
                "x_coords": len(xs),
                "z_coords": len(zs),
                "cells": len(floor_cells),
            }
        blocker_rects = filter_wall_rects(blocker_rects, args.min_wall_length)
        projection = {
            "source": "nav",
            "nav": {
                "path": nav_projection["path"],
                "version": nav_projection["version"],
                "subversion": nav_projection["subversion"],
                "area_count": nav_projection["area_count"],
                "bytes_remaining_after_areas": nav_projection["bytes_remaining_after_areas"],
                "union": nav_union_stats,
            },
        }
    else:
        floor_cells, blocker_cells, projection = extract_face_projection_cells(
            args.bsp,
            args.floor_cell_size,
            args.blocker_cell_size,
            args.min_floor_area,
            args.min_wall_area,
            args.min_wall_height,
            args.wall_expand,
        )
        if args.no_blockers:
            blocker_cells = set()
        floor_rects = merge_cells_to_rects(floor_cells, args.floor_cell_size)
        nav_graph = make_uniform_nav_graph(floor_cells, args.floor_cell_size)
        blocker_rects = filter_wall_rects(merge_cells_to_rects(blocker_cells, args.blocker_cell_size), args.min_wall_length)
        projection["source"] = "faces"

        def point_on_floor(x, z):
            return point_in_cells(x, z, floor_cells, args.floor_cell_size)

    perimeter_rects = []
    floor_bounds = rects_bounds(floor_rects)
    if floor_bounds and not use_nav and not args.no_perimeter_walls:
        perimeter_rects = perimeter_wall_rects(
            floor_bounds,
            args.perimeter_padding,
            args.perimeter_thickness,
        )

    entities = load_entities(args.extracted)
    models = load_models(args.extracted)
    spawns, spawn_yaws, spawn_entities = build_spawns(entities, args.ground_y, args.spawn_height)
    spawn_points = spawn_clearance_points(spawn_entities)
    bombsites = build_bombsites(entities, models)
    protected_points = spawn_points + bombsite_centers(bombsites)
    protected_nav_cells = {
        cell
        for cell in (nav_cell_for_point(nav_graph, point[0], point[1]) for point in protected_points)
        if cell is not None
    }
    blocked_nav_cells = set()
    asset_height_filter = build_asset_height_filter(
        nav_projection,
        args.asset_height_tolerance,
        args.asset_height_search_distance,
    )

    blocker_rect_count_before_spawn_clearance = len(blocker_rects)
    perimeter_rect_count_before_spawn_clearance = len(perimeter_rects)
    blocker_rects = [
        rect for rect in blocker_rects if not rect_hits_spawn_clearance(rect, spawn_points, args.spawn_wall_clearance)
    ]
    perimeter_rects = [
        rect for rect in perimeter_rects if not rect_hits_spawn_clearance(rect, spawn_points, args.spawn_wall_clearance)
    ]
    containment_seal_rects = []
    containment_seal_enabled = use_nav and args.nav_floor_mode == "grid" and not args.no_blockers and not args.no_containment_seal_walls
    if containment_seal_enabled:
        seal_wall_thickness = (
            args.containment_seal_wall_thickness
            if args.containment_seal_wall_thickness is not None
            else min(args.boundary_wall_thickness, args.containment_seal_edge_depth)
        )
        containment_seal_rects = containment_seal_wall_rects(
            floor_rects,
            blocker_rects + perimeter_rects,
            args.containment_seal_grid_size,
            args.containment_seal_edge_depth,
            seal_wall_thickness,
            args.min_wall_length,
        )
        containment_seal_rects = [
            rect for rect in containment_seal_rects if not rect_hits_spawn_clearance(rect, spawn_points, args.spawn_wall_clearance)
        ]

    floor_color = color_palette["floor"]
    blocker_color = color_palette["wall"]
    roof_color = color_palette["roof"]
    solids = [
        source_rect_to_js_solid(
            rect,
            args.ground_y - args.floor_thickness,
            args.ground_y,
            floor_color,
            "floor",
        )
        for rect in floor_rects
    ]
    solids.extend(
        source_rect_to_js_solid(
            rect,
            args.ground_y,
            args.ground_y + args.blocker_height,
            blocker_color,
            "wall",
        )
        for rect in blocker_rects
    )
    solids.extend(
        source_rect_to_js_solid(
            rect,
            args.ground_y,
            args.ground_y + args.blocker_height,
            blocker_color,
            "wall",
        )
        for rect in perimeter_rects
    )
    solids.extend(
        source_rect_to_js_solid(
            rect,
            args.ground_y,
            args.ground_y + args.blocker_height,
            blocker_color,
            "wall",
        )
        for rect in containment_seal_rects
    )
    roof_rects = []
    if args.include_roof:
        roof_bottom = args.ground_y + (args.roof_height if args.roof_height is not None else args.blocker_height)
        roof_top = roof_bottom + args.roof_thickness
        roof_rects = [expand_rect(rect, args.roof_padding) for rect in floor_rects]
        solids.extend(
            source_rect_to_js_solid(
                rect,
                roof_bottom,
                roof_top,
                roof_color,
                "wall",
            )
            for rect in roof_rects
        )

    asset_solids = []
    asset_metadata = {}
    if args.include_assets:
        if args.asset_source in {"entities", "both"}:
            entity_asset_solids, entity_asset_meta = build_entity_asset_solids(
                entities,
                point_on_floor,
                args.ground_y,
                spawn_points,
                args.spawn_clearance,
                args.extracted,
                args.asset_bounds,
                allowed_asset_types,
                nav_graph,
                blocked_nav_cells,
                protected_nav_cells,
                args.player_radius,
                not args.no_protect_nav_connectivity,
                args.max_asset_nav_cells,
                color_palette,
                asset_height_filter,
            )
            asset_solids.extend(entity_asset_solids)
            asset_metadata["entities"] = entity_asset_meta
        if args.asset_source in {"static", "both"}:
            static_asset_solids, static_asset_meta = build_static_asset_solids(
                args.extracted,
                point_on_floor,
                args.ground_y,
                static_asset_limit,
                spawn_points,
                args.spawn_clearance,
                args.asset_bounds,
                allowed_asset_types,
                nav_graph,
                blocked_nav_cells,
                protected_nav_cells,
                args.player_radius,
                not args.no_protect_nav_connectivity,
                args.max_asset_nav_cells,
                color_palette,
                asset_height_filter,
            )
            asset_solids.extend(static_asset_solids)
            asset_metadata["static"] = static_asset_meta
        solids.extend(asset_solids)

    first_spawn = spawn_entities["t"][0] if spawn_entities["t"] else None
    if first_spawn:
        source = parse_vec3(first_spawn["origin"])
        surf_start = clean_vec([source[0], args.ground_y + args.spawn_height, source[1], parse_yaw(first_spawn)])
    else:
        surf_start = [0, clean_num(args.ground_y + args.spawn_height), 0, 0]

    surf_finish = {"min": [0, 0], "max": [0, 0]}
    if bombsites:
        surf_finish = {"min": bombsites[0]["min"], "max": bombsites[0]["max"]}

    output = {
        "name": args.name,
        "title": args.title,
        "theme": args.theme,
        "mode": "defusal",
        "killY": clean_num(args.ground_y - args.kill_y_below_ground),
        "solids": solids,
        "ramps": [],
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
    scale_map_output(output, args.scale)
    engine_runtime = add_engine_runtime_fields(output)

    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(output, separators=(",", ":")) + "\n", encoding="utf-8")

    metadata = {
        "source_bsp": str(args.bsp),
        "source_extracted_dir": str(args.extracted),
        "coordinate_transform": "source [x,y,z] -> flat js [x,groundY,z/sourceY]",
        "note": "All vertical map variation is flattened. Nav mode uses nav areas for floors and boundary walls around the walkable footprint. Ramps and ladders are omitted. Props are omitted unless --include-assets is set, then approximated as box solids.",
        "parameters": {
            "scale": args.scale,
            "color_mode": args.color_mode,
            "ground_y": args.ground_y,
            "floor_thickness": args.floor_thickness,
            "blocker_height": args.blocker_height,
            "kill_y_below_ground": args.kill_y_below_ground,
            "roof_enabled": args.include_roof,
            "roof_height": args.roof_height if args.roof_height is not None else args.blocker_height,
            "roof_thickness": args.roof_thickness,
            "roof_padding": args.roof_padding,
            "floor_cell_size": args.floor_cell_size,
            "blocker_cell_size": args.blocker_cell_size,
            "nav_cell_size": args.nav_cell_size,
            "nav_floor_mode": args.nav_floor_mode if use_nav else None,
            "nav_snap_size": args.nav_snap_size if use_nav else None,
            "boundary_wall_thickness": args.boundary_wall_thickness,
            "min_wall_length": args.min_wall_length,
            "containment_seal_walls_enabled": containment_seal_enabled,
            "containment_seal_grid_size": args.containment_seal_grid_size,
            "containment_seal_edge_depth": args.containment_seal_edge_depth,
            "containment_seal_wall_thickness": (
                args.containment_seal_wall_thickness
                if args.containment_seal_wall_thickness is not None
                else min(args.boundary_wall_thickness, args.containment_seal_edge_depth)
            ),
            "min_floor_area": args.min_floor_area,
            "min_wall_area": args.min_wall_area,
            "min_wall_height": args.min_wall_height,
            "wall_expand": args.wall_expand,
            "blockers_enabled": not args.no_blockers,
            "boundary_walls_enabled": use_nav and not args.no_blockers,
            "perimeter_walls_enabled": (not use_nav) and not args.no_perimeter_walls,
            "perimeter_padding": args.perimeter_padding,
            "perimeter_thickness": args.perimeter_thickness,
            "flat_source": "nav" if use_nav else "faces",
            "assets_enabled": args.include_assets,
            "asset_source": args.asset_source,
            "asset_density": args.asset_density,
            "asset_types": "all" if allowed_asset_types is None else sorted(allowed_asset_types),
            "asset_bounds": args.asset_bounds,
            "asset_floor_tolerance": args.asset_floor_tolerance,
            "asset_height_tolerance": args.asset_height_tolerance,
            "asset_height_search_distance": args.asset_height_search_distance,
            "static_asset_limit": static_asset_limit,
            "spawn_clearance": args.spawn_clearance,
            "spawn_wall_clearance": args.spawn_wall_clearance,
            "player_radius": args.player_radius,
            "protect_nav_connectivity": not args.no_protect_nav_connectivity,
            "max_asset_nav_cells": args.max_asset_nav_cells,
            "include_spawn_yaw": args.include_spawn_yaw,
        },
        "output_counts": {
            "floor_rects": len(floor_rects),
            "blocker_rects": len(blocker_rects),
            "perimeter_wall_rects": len(perimeter_rects),
            "containment_seal_wall_rects": len(containment_seal_rects),
            "roof_rects": len(roof_rects),
            "blocker_rects_removed_for_spawn_clearance": blocker_rect_count_before_spawn_clearance - len(blocker_rects),
            "perimeter_wall_rects_removed_for_spawn_clearance": perimeter_rect_count_before_spawn_clearance - len(perimeter_rects),
            "solids": len(solids),
            "asset_solids": len(asset_solids),
            "ramps": 0,
            "t_spawns": len(spawns["t"]),
            "ct_spawns": len(spawns["ct"]),
            "bombsites": len(bombsites),
            "ladders": 0,
            "protected_nav_cells": len(protected_nav_cells),
            "asset_blocked_nav_cells": len(blocked_nav_cells),
        },
        "spawn_yaws": spawn_yaws,
        "colors": {
            "palette": color_palette,
            "hex": {role: f"#{color:06x}" for role, color in color_palette.items()},
            "inference": color_metadata,
        },
        "assets": asset_metadata,
        "projection": projection,
        "engine_runtime": engine_runtime,
    }
    args.out.with_suffix(".meta.json").write_text(json.dumps(metadata, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(metadata["output_counts"], indent=2))


if __name__ == "__main__":
    main()
