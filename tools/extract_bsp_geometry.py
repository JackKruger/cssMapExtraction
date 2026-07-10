#!/usr/bin/env python3
"""Extract Source/VBSP geometry and metadata from a BSP map.

This targets the geometry-bearing lumps in Source BSP version 20 maps, including
brush faces, brush side planes, displacement meshes, material references, entity
metadata, embedded pakfile resources, and basic static prop placement metadata.
"""

from __future__ import annotations

import argparse
import collections
import hashlib
import io
import json
import math
import os
from pathlib import Path
import re
import struct
import zipfile


LUMP_NAMES = {
    0: "entities",
    1: "planes",
    2: "texdata",
    3: "vertexes",
    4: "visibility",
    5: "nodes",
    6: "texinfo",
    7: "faces",
    8: "lighting",
    9: "occlusion",
    10: "leafs",
    11: "faceids",
    12: "edges",
    13: "surfedges",
    14: "models",
    15: "worldlights",
    16: "leaffaces",
    17: "leafbrushes",
    18: "brushes",
    19: "brushsides",
    20: "areas",
    21: "areaportals",
    26: "dispinfo",
    27: "originalfaces",
    28: "physdisp",
    29: "physcollide",
    30: "vertnormals",
    31: "vertnormalindices",
    32: "displightmapalphas",
    33: "dispverts",
    34: "displightmapsamplepositions",
    35: "gamelump",
    37: "primitives",
    38: "primverts",
    39: "primindices",
    40: "pakfile",
    41: "clipportalverts",
    42: "cubemaps",
    43: "texdatastringdata",
    44: "texdatastringtable",
    45: "overlays",
    46: "leafmindisttowater",
    47: "facemacrotextureinfo",
    48: "disptris",
    49: "physcollidesurface",
    52: "lighting_hdr",
    53: "worldlights_hdr",
    58: "faces_hdr",
}

LUMP_RECORD_SIZES = {
    1: 20,
    2: 32,
    3: 12,
    5: 32,
    6: 72,
    7: 56,
    10: 32,
    12: 4,
    13: 4,
    14: 48,
    16: 2,
    17: 2,
    18: 12,
    19: 8,
    20: 8,
    21: 12,
    26: 176,
    27: 56,
    30: 12,
    31: 2,
    33: 20,
    37: 10,
    39: 2,
    42: 16,
    44: 4,
    45: 352,
    47: 2,
    48: 2,
}

FACE_FLAGS = {
    0x0001: "LIGHT",
    0x0002: "SKY2D",
    0x0004: "SKY",
    0x0008: "WARP",
    0x0010: "TRANS",
    0x0020: "NOPORTAL",
    0x0040: "TRIGGER",
    0x0080: "NODRAW",
    0x0100: "HINT",
    0x0200: "SKIP",
    0x0400: "NOLIGHT",
    0x0800: "BUMPLIGHT",
    0x1000: "NOSHADOWS",
    0x2000: "NODECALS",
    0x4000: "NOCHOP",
    0x8000: "HITBOX",
}


def unpack(fmt: str, data: bytes, offset: int = 0):
    return struct.unpack_from(fmt, data, offset)


def read_c_string(data: bytes, offset: int) -> str:
    if offset < 0 or offset >= len(data):
        return ""
    end = data.find(b"\0", offset)
    if end == -1:
        end = len(data)
    return data[offset:end].decode("utf-8", errors="replace")


def ensure_dir(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True)


def write_json(path: Path, value) -> None:
    ensure_dir(path.parent)
    path.write_text(json.dumps(value, indent=2, sort_keys=False) + "\n", encoding="utf-8")


def bounds_for_points(points):
    if not points:
        return {"mins": [0, 0, 0], "maxs": [0, 0, 0]}
    mins = [min(p[i] for p in points) for i in range(3)]
    maxs = [max(p[i] for p in points) for i in range(3)]
    return {"mins": mins, "maxs": maxs}


def vec_add(a, b):
    return [a[0] + b[0], a[1] + b[1], a[2] + b[2]]


def vec_mul(a, s: float):
    return [a[0] * s, a[1] * s, a[2] * s]


def vec_dist2(a, b) -> float:
    return sum((a[i] - b[i]) ** 2 for i in range(3))


def sanitize_mtl_name(material: str | None, fallback: str) -> str:
    if not material:
        material = fallback
    name = re.sub(r"[^A-Za-z0-9_.-]+", "_", material)
    return name[:120] or fallback


def deterministic_color(name: str):
    digest = hashlib.sha1(name.encode("utf-8", errors="replace")).digest()
    return [round(0.2 + (digest[i] / 255.0) * 0.65, 4) for i in range(3)]


class Bsp:
    def __init__(self, path: Path):
        self.path = path
        self.data = path.read_bytes()
        if len(self.data) < 1036:
            raise ValueError(f"{path} is too small to be a VBSP file")
        self.ident = self.data[:4].decode("ascii", errors="replace")
        self.version = unpack("<i", self.data, 4)[0]
        self.map_revision = unpack("<i", self.data, 4 + 4 + 64 * 16)[0]
        self.lumps = []
        for i in range(64):
            fileofs, filelen, version, fourcc = unpack("<iiII", self.data, 8 + i * 16)
            self.lumps.append(
                {
                    "id": i,
                    "name": LUMP_NAMES.get(i, f"unknown_{i}"),
                    "offset": fileofs,
                    "length": filelen,
                    "version": version,
                    "fourcc": fourcc,
                    "record_size": LUMP_RECORD_SIZES.get(i),
                    "record_count": (
                        filelen // LUMP_RECORD_SIZES[i]
                        if i in LUMP_RECORD_SIZES and filelen % LUMP_RECORD_SIZES[i] == 0
                        else None
                    ),
                }
            )

    def lump(self, lump_id: int) -> bytes:
        item = self.lumps[lump_id]
        return self.data[item["offset"] : item["offset"] + item["length"]]


def parse_entities(text: str):
    entities = []
    current = None
    pair_re = re.compile(r'"((?:\\.|[^"\\])*)"\s+"((?:\\.|[^"\\])*)"')
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("//"):
            continue
        if stripped == "{":
            current = {}
            continue
        if stripped == "}":
            if current is not None:
                entities.append(current)
            current = None
            continue
        if current is None:
            continue
        match = pair_re.search(stripped)
        if match:
            key = match.group(1).replace(r"\"", '"')
            value = match.group(2).replace(r"\"", '"')
            current[key] = value
    return entities


def parse_planes(bsp: Bsp):
    data = bsp.lump(1)
    planes = []
    for off in range(0, len(data), 20):
        normal = list(unpack("<3f", data, off))
        dist, ptype = unpack("<fi", data, off + 12)
        planes.append({"normal": normal, "dist": dist, "type": ptype})
    return planes


def parse_vertices(bsp: Bsp):
    data = bsp.lump(3)
    return [list(unpack("<3f", data, off)) for off in range(0, len(data), 12)]


def parse_edges(bsp: Bsp):
    data = bsp.lump(12)
    return [list(unpack("<HH", data, off)) for off in range(0, len(data), 4)]


def parse_surfedges(bsp: Bsp):
    data = bsp.lump(13)
    return [unpack("<i", data, off)[0] for off in range(0, len(data), 4)]


def parse_texdata_and_materials(bsp: Bsp):
    texdata_bytes = bsp.lump(2)
    string_data = bsp.lump(43)
    table_bytes = bsp.lump(44)
    string_offsets = [unpack("<i", table_bytes, off)[0] for off in range(0, len(table_bytes), 4)]
    texdata = []
    materials = []
    for i, off in enumerate(range(0, len(texdata_bytes), 32)):
        reflectivity = list(unpack("<3f", texdata_bytes, off))
        name_id, width, height, view_width, view_height = unpack("<iiiii", texdata_bytes, off + 12)
        material = ""
        if 0 <= name_id < len(string_offsets):
            material = read_c_string(string_data, string_offsets[name_id])
        item = {
            "id": i,
            "material": material,
            "reflectivity": reflectivity,
            "width": width,
            "height": height,
            "view_width": view_width,
            "view_height": view_height,
            "name_string_table_id": name_id,
        }
        texdata.append(item)
        materials.append(material)
    return texdata, materials


def parse_texinfo(bsp: Bsp):
    data = bsp.lump(6)
    texinfo = []
    for i, off in enumerate(range(0, len(data), 72)):
        texture_vecs = [list(unpack("<4f", data, off)), list(unpack("<4f", data, off + 16))]
        lightmap_vecs = [list(unpack("<4f", data, off + 32)), list(unpack("<4f", data, off + 48))]
        flags, texdata = unpack("<ii", data, off + 64)
        texinfo.append(
            {
                "id": i,
                "texture_vecs": texture_vecs,
                "lightmap_vecs": lightmap_vecs,
                "flags": flags,
                "flag_names": [name for bit, name in FACE_FLAGS.items() if flags & bit],
                "texdata": texdata,
            }
        )
    return texinfo


def parse_faces(bsp: Bsp, lump_id: int = 7):
    data = bsp.lump(lump_id)
    faces = []
    for i, off in enumerate(range(0, len(data), 56)):
        (
            planenum,
            side,
            on_node,
            firstedge,
            numedges,
            texinfo,
            dispinfo,
            surface_fog_volume_id,
        ) = unpack("<HBBihhhh", data, off)
        styles = list(unpack("<4B", data, off + 16))
        lightofs = unpack("<i", data, off + 20)[0]
        area = unpack("<f", data, off + 24)[0]
        lightmap_mins = list(unpack("<2i", data, off + 28))
        lightmap_size = list(unpack("<2i", data, off + 36))
        orig_face = unpack("<i", data, off + 44)[0]
        num_prims, first_prim_id = unpack("<HH", data, off + 48)
        smoothing_groups = unpack("<I", data, off + 52)[0]
        faces.append(
            {
                "id": i,
                "planenum": planenum,
                "side": side,
                "on_node": on_node,
                "firstedge": firstedge,
                "numedges": numedges,
                "texinfo": texinfo,
                "dispinfo": dispinfo,
                "surface_fog_volume_id": surface_fog_volume_id,
                "styles": styles,
                "lightofs": lightofs,
                "area": area,
                "lightmap_mins": lightmap_mins,
                "lightmap_size": lightmap_size,
                "orig_face": orig_face,
                "num_prims": num_prims,
                "first_prim_id": first_prim_id,
                "smoothing_groups": smoothing_groups,
            }
        )
    return faces


def parse_models(bsp: Bsp):
    data = bsp.lump(14)
    models = []
    for i, off in enumerate(range(0, len(data), 48)):
        mins = list(unpack("<3f", data, off))
        maxs = list(unpack("<3f", data, off + 12))
        origin = list(unpack("<3f", data, off + 24))
        headnode, firstface, numfaces = unpack("<iii", data, off + 36)
        models.append(
            {
                "id": i,
                "name": "worldspawn" if i == 0 else f"*{i}",
                "mins": mins,
                "maxs": maxs,
                "origin": origin,
                "headnode": headnode,
                "firstface": firstface,
                "numfaces": numfaces,
            }
        )
    return models


def parse_brushes_and_sides(bsp: Bsp, planes, texinfo, texdata):
    brushes_data = bsp.lump(18)
    sides_data = bsp.lump(19)
    sides = []
    for i, off in enumerate(range(0, len(sides_data), 8)):
        planenum, texinfo_id, dispinfo, bevel = unpack("<Hhhh", sides_data, off)
        material = material_for_texinfo(texinfo_id, texinfo, texdata)
        sides.append(
            {
                "id": i,
                "planenum": planenum,
                "plane": planes[planenum] if 0 <= planenum < len(planes) else None,
                "texinfo": texinfo_id,
                "material": material,
                "dispinfo": dispinfo,
                "bevel": bevel,
            }
        )
    brushes = []
    for i, off in enumerate(range(0, len(brushes_data), 12)):
        firstside, numsides, contents = unpack("<iii", brushes_data, off)
        brushes.append(
            {
                "id": i,
                "firstside": firstside,
                "numsides": numsides,
                "contents": contents,
                "sides": sides[firstside : firstside + numsides],
            }
        )
    return brushes, sides


def parse_dispinfos(bsp: Bsp):
    data = bsp.lump(26)
    dispinfos = []
    for i, off in enumerate(range(0, len(data), 176)):
        start_position = list(unpack("<3f", data, off))
        disp_vert_start, disp_tri_start, power, min_tess = unpack("<iiii", data, off + 12)
        smoothing_angle = unpack("<f", data, off + 28)[0]
        contents = unpack("<i", data, off + 32)[0]
        map_face = unpack("<H", data, off + 36)[0]
        # LightmapAlphaStart and LightmapSamplePositionStart follow after padding.
        lightmap_alpha_start, lightmap_sample_position_start = unpack("<ii", data, off + 40)
        allowed_verts = list(unpack("<10I", data, off + 136))
        dispinfos.append(
            {
                "id": i,
                "start_position": start_position,
                "disp_vert_start": disp_vert_start,
                "disp_tri_start": disp_tri_start,
                "power": power,
                "min_tess": min_tess,
                "smoothing_angle": smoothing_angle,
                "contents": contents,
                "map_face": map_face,
                "lightmap_alpha_start": lightmap_alpha_start,
                "lightmap_sample_position_start": lightmap_sample_position_start,
                "allowed_verts": allowed_verts,
            }
        )
    return dispinfos


def parse_dispverts(bsp: Bsp):
    data = bsp.lump(33)
    verts = []
    for off in range(0, len(data), 20):
        vector = list(unpack("<3f", data, off))
        dist, alpha = unpack("<ff", data, off + 12)
        verts.append({"vector": vector, "dist": dist, "alpha": alpha})
    return verts


def parse_gamelumps(bsp: Bsp):
    data = bsp.lump(35)
    if len(data) < 4:
        return []
    count = unpack("<i", data, 0)[0]
    entries = []
    for i in range(count):
        off = 4 + i * 16
        if off + 16 > len(data):
            break
        raw_id, flags, version, fileofs, filelen = unpack("<IHHii", data, off)
        id_bytes = struct.pack("<I", raw_id)
        id_forward = id_bytes.decode("latin1", errors="replace")
        id_reversed = id_bytes[::-1].decode("latin1", errors="replace")
        item = {
            "id": i,
            "raw_id": raw_id,
            "id_bytes": list(id_bytes),
            "id_forward": id_forward,
            "id_reversed": id_reversed,
            "name": id_reversed if id_reversed == "sprp" else id_forward,
            "flags": flags,
            "version": version,
            "offset": fileofs,
            "length": filelen,
        }
        if item["name"] == "sprp":
            item["static_props"] = parse_static_prop_gamelump(bsp, fileofs, filelen)
        entries.append(item)
    return entries


def parse_static_prop_gamelump(bsp: Bsp, fileofs: int, filelen: int):
    data = bsp.data[fileofs : fileofs + filelen]
    result = {}
    cursor = 0
    if len(data) < 4:
        return result
    dict_count = unpack("<i", data, cursor)[0]
    cursor += 4
    names = []
    for _ in range(max(0, dict_count)):
        if cursor + 128 > len(data):
            break
        raw = data[cursor : cursor + 128]
        names.append(raw.split(b"\0", 1)[0].decode("utf-8", errors="replace"))
        cursor += 128
    result["model_dictionary_count"] = dict_count
    result["model_dictionary"] = names
    if cursor + 4 > len(data):
        return result
    leaf_count = unpack("<i", data, cursor)[0]
    cursor += 4 + max(0, leaf_count) * 2
    result["leaf_count"] = leaf_count
    if cursor + 4 > len(data):
        return result
    prop_count = unpack("<i", data, cursor)[0]
    cursor += 4
    remaining = len(data) - cursor
    entry_size = remaining // prop_count if prop_count > 0 and remaining % prop_count == 0 else None
    result["prop_count"] = prop_count
    result["inferred_prop_entry_size"] = entry_size
    props = []
    if prop_count > 0 and entry_size and entry_size >= 34:
        for prop_id in range(prop_count):
            off = cursor + prop_id * entry_size
            origin = list(unpack("<3f", data, off))
            angles = list(unpack("<3f", data, off + 12))
            prop_type, first_leaf, leaf_count_for_prop = unpack("<HHH", data, off + 24)
            solid, flags = unpack("<BB", data, off + 30)
            skin = unpack("<i", data, off + 32)[0] if entry_size >= 36 else None
            fade_min = unpack("<f", data, off + 36)[0] if entry_size >= 40 else None
            fade_max = unpack("<f", data, off + 40)[0] if entry_size >= 44 else None
            lighting_origin = list(unpack("<3f", data, off + 44)) if entry_size >= 56 else None
            item = {
                "id": prop_id,
                "origin": origin,
                "angles": angles,
                "prop_type": prop_type,
                "model": names[prop_type] if 0 <= prop_type < len(names) else None,
                "first_leaf": first_leaf,
                "leaf_count": leaf_count_for_prop,
                "solid": solid,
                "flags": flags,
                "skin": skin,
                "fade_min_dist": fade_min,
                "fade_max_dist": fade_max,
                "lighting_origin": lighting_origin,
            }
            if entry_size >= 60:
                item["forced_fade_scale"] = unpack("<f", data, off + 56)[0]
            if entry_size >= 64:
                min_dx, max_dx = unpack("<HH", data, off + 60)
                item["min_dx_level"] = min_dx
                item["max_dx_level"] = max_dx
            props.append(item)
    result["props"] = props
    result["sample_props"] = props[:50]
    return result


def material_for_texinfo(texinfo_id: int, texinfo, texdata) -> str | None:
    if texinfo_id < 0 or texinfo_id >= len(texinfo):
        return None
    texdata_id = texinfo[texinfo_id]["texdata"]
    if texdata_id < 0 or texdata_id >= len(texdata):
        return None
    return texdata[texdata_id]["material"]


def face_vertex_indices(face, edges, surfedges):
    indices = []
    for i in range(face["numedges"]):
        surfedge_index = face["firstedge"] + i
        if surfedge_index < 0 or surfedge_index >= len(surfedges):
            continue
        surfedge = surfedges[surfedge_index]
        edge_index = abs(surfedge)
        if edge_index < 0 or edge_index >= len(edges):
            continue
        edge = edges[edge_index]
        indices.append(edge[0] if surfedge >= 0 else edge[1])
    return indices


def model_lookup_for_faces(models):
    lookup = {}
    for model in models:
        for face_id in range(model["firstface"], model["firstface"] + model["numfaces"]):
            lookup[face_id] = model["id"]
    return lookup


def write_materials_mtl(path: Path, material_to_mtl: dict[str, str]) -> None:
    ensure_dir(path.parent)
    lines = []
    for material, mtl in sorted(material_to_mtl.items(), key=lambda item: item[1]):
        color = deterministic_color(material)
        lines.extend(
            [
                f"# {material}",
                f"newmtl {mtl}",
                f"Kd {color[0]} {color[1]} {color[2]}",
                "Ka 0 0 0",
                "Ks 0 0 0",
                "",
            ]
        )
    path.write_text("\n".join(lines), encoding="utf-8")


def export_brush_faces_obj(path: Path, vertices, enriched_faces, material_to_mtl):
    ensure_dir(path.parent)
    with path.open("w", encoding="utf-8") as f:
        f.write("# Brush face mesh exported from BSP faces. Displacement faces are omitted here.\n")
        f.write("mtllib materials.mtl\n")
        for vertex in vertices:
            f.write(f"v {vertex[0]:.6f} {vertex[1]:.6f} {vertex[2]:.6f}\n")
        current_group = None
        current_material = None
        for face in enriched_faces:
            if face["dispinfo"] >= 0 or len(face["vertex_indices"]) < 3:
                continue
            group = f"model_{face['model_id']:03d}"
            if group != current_group:
                f.write(f"g {group}\n")
                current_group = group
            material = face.get("material") or "__unknown__"
            mtl = material_to_mtl.setdefault(material, sanitize_mtl_name(material, f"mat_{len(material_to_mtl)}"))
            if mtl != current_material:
                f.write(f"usemtl {mtl}\n")
                current_material = mtl
            idxs = [str(i + 1) for i in face["vertex_indices"]]
            f.write("f " + " ".join(idxs) + "\n")


def base_displacement_corners(face, vertices):
    idxs = face["vertex_indices"]
    if len(idxs) < 4:
        return []
    # Displacement faces are expected to be quads. If a malformed face has more
    # points, preserve the first four in winding order.
    return [vertices[i] for i in idxs[:4]]


def reorder_corners_from_start(corners, start_position):
    if len(corners) != 4:
        return corners
    start = min(range(4), key=lambda i: vec_dist2(corners[i], start_position))
    return [corners[(start + i) % 4] for i in range(4)]


def bilinear(corners, s: float, t: float):
    out = [0.0, 0.0, 0.0]
    weights = [(1 - s) * (1 - t), s * (1 - t), s * t, (1 - s) * t]
    for corner, weight in zip(corners, weights):
        out = vec_add(out, vec_mul(corner, weight))
    return out


def build_displacement_mesh(enriched_faces, dispinfos, dispverts, vertices):
    obj_vertices = []
    obj_faces = []
    disp_records = []
    for face in enriched_faces:
        disp_id = face["dispinfo"]
        if disp_id < 0 or disp_id >= len(dispinfos):
            continue
        disp = dispinfos[disp_id]
        power = disp["power"]
        side = (1 << power) + 1
        expected_verts = side * side
        if disp["disp_vert_start"] < 0 or disp["disp_vert_start"] + expected_verts > len(dispverts):
            continue
        corners = base_displacement_corners(face, vertices)
        if len(corners) != 4:
            disp_records.append(
                {
                    "id": disp_id,
                    "face_id": face["id"],
                    "status": "skipped_non_quad_base_face",
                    "base_vertex_count": len(corners),
                }
            )
            continue
        corners = reorder_corners_from_start(corners, disp["start_position"])
        base_index = len(obj_vertices) + 1
        for y in range(side):
            t = y / (side - 1) if side > 1 else 0.0
            for x in range(side):
                s = x / (side - 1) if side > 1 else 0.0
                dispvert = dispverts[disp["disp_vert_start"] + y * side + x]
                base_pos = bilinear(corners, s, t)
                displaced = vec_add(base_pos, vec_mul(dispvert["vector"], dispvert["dist"]))
                obj_vertices.append(displaced)
        for y in range(side - 1):
            for x in range(side - 1):
                v00 = base_index + y * side + x
                v10 = base_index + y * side + x + 1
                v01 = base_index + (y + 1) * side + x
                v11 = base_index + (y + 1) * side + x + 1
                obj_faces.append(
                    {
                        "face_id": face["id"],
                        "dispinfo": disp_id,
                        "material": face.get("material") or "__unknown__",
                        "triangles": [(v00, v10, v11), (v00, v11, v01)],
                    }
                )
        disp_records.append(
            {
                "id": disp_id,
                "face_id": face["id"],
                "power": power,
                "side_vertices": side,
                "vertex_count": expected_verts,
                "quad_count": (side - 1) * (side - 1),
                "triangle_count": (side - 1) * (side - 1) * 2,
                "material": face.get("material"),
                "bounds": bounds_for_points(obj_vertices[base_index - 1 : base_index - 1 + expected_verts]),
            }
        )
    return obj_vertices, obj_faces, disp_records


def export_displacements_obj(path: Path, vertices, faces, material_to_mtl):
    ensure_dir(path.parent)
    with path.open("w", encoding="utf-8") as f:
        f.write("# Displacement mesh reconstructed from BSP dispinfo/dispverts.\n")
        f.write("mtllib materials.mtl\n")
        for vertex in vertices:
            f.write(f"v {vertex[0]:.6f} {vertex[1]:.6f} {vertex[2]:.6f}\n")
        current_material = None
        current_disp = None
        for item in faces:
            if item["dispinfo"] != current_disp:
                f.write(f"g displacement_{item['dispinfo']:04d}_face_{item['face_id']:05d}\n")
                current_disp = item["dispinfo"]
            material = item["material"]
            mtl = material_to_mtl.setdefault(material, sanitize_mtl_name(material, f"mat_{len(material_to_mtl)}"))
            if mtl != current_material:
                f.write(f"usemtl {mtl}\n")
                current_material = mtl
            for tri in item["triangles"]:
                f.write(f"f {tri[0]} {tri[1]} {tri[2]}\n")


def safe_pak_target(root: Path, normalized: str) -> Path:
    """Resolve a pakfile entry under root, rejecting path-traversal escapes.

    A plain string prefix check is not enough: a sibling directory like
    ``root_evil`` would satisfy ``startswith(str(root))``. Requiring root to be
    the target itself or one of its parents closes that hole.
    """
    root = root.resolve()
    target = (root / normalized).resolve()
    if target != root and root not in target.parents:
        raise ValueError(f"unsafe pakfile path: {normalized}")
    return target


def safe_extract_zip(zip_file: zipfile.ZipFile, destination: Path):
    ensure_dir(destination)
    root = destination.resolve()
    for info in zip_file.infolist():
        normalized = info.filename.replace("\\", "/")
        target = safe_pak_target(root, normalized)
        if info.is_dir():
            ensure_dir(target)
            continue
        ensure_dir(target.parent)
        target.write_bytes(zip_file.read(info))


def unpack_pakfile(bsp: Bsp, out_dir: Path, mode: str):
    pak = bsp.lump(40)
    manifest = {
        "offset": bsp.lumps[40]["offset"],
        "length": len(pak),
        "valid_zip": False,
        "entries": [],
        "counts_by_top_level": {},
        "counts_by_extension": {},
    }
    if not pak:
        write_json(out_dir / "pakfile_manifest.json", manifest)
        return manifest
    with zipfile.ZipFile(io.BytesIO(pak)) as zf:
        manifest["valid_zip"] = True
        top_counts = collections.Counter()
        ext_counts = collections.Counter()
        entries = []
        for info in zf.infolist():
            normalized = info.filename.replace("\\", "/")
            top_counts[normalized.split("/", 1)[0]] += 1
            suffix = Path(normalized).suffix.lower().lstrip(".") or "<none>"
            ext_counts[suffix] += 1
            entries.append(
                {
                    "path": normalized,
                    "compressed_size": info.compress_size,
                    "size": info.file_size,
                    "crc": f"{info.CRC:08x}",
                }
            )
        manifest["entries"] = entries
        manifest["counts_by_top_level"] = dict(top_counts)
        manifest["counts_by_extension"] = dict(ext_counts)
        manifest["entry_count"] = len(entries)
        manifest["uncompressed_size"] = sum(item["size"] for item in entries)
        manifest["compressed_size"] = sum(item["compressed_size"] for item in entries)
        write_json(out_dir / "pakfile_manifest.json", manifest)

        if mode in {"text", "all"}:
            text_root = (out_dir / "pakfile_text").resolve()
            for info in zf.infolist():
                normalized = info.filename.replace("\\", "/")
                if normalized.lower().endswith((".txt", ".res", ".cfg", ".vmt")) and mode == "text":
                    target = safe_pak_target(text_root, normalized)
                    ensure_dir(target.parent)
                    target.write_bytes(zf.read(info))
            if mode == "all":
                safe_extract_zip(zf, out_dir / "pakfile_files")
    return manifest


def parse_nav_header(nav_path: Path):
    if not nav_path or not nav_path.exists():
        return None
    data = nav_path.read_bytes()
    if len(data) < 20:
        return {"path": str(nav_path), "size": len(data), "error": "too_small"}
    magic, version, subversion, bsp_size, analyzed = unpack("<IIIII", data, 0)
    return {
        "path": str(nav_path),
        "size": len(data),
        "magic": f"0x{magic:08x}",
        "version": version,
        "subversion": subversion,
        "stored_bsp_size": bsp_size,
        "analyzed_flag": analyzed,
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("bsp", type=Path)
    parser.add_argument("--nav", type=Path)
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument(
        "--pak",
        choices=["none", "index", "text", "all"],
        default="text",
        help="How much of the embedded pakfile to extract.",
    )
    args = parser.parse_args()

    bsp = Bsp(args.bsp)
    out = args.out
    ensure_dir(out)
    ensure_dir(out / "geometry")

    write_json(
        out / "bsp_header.json",
        {
            "path": str(args.bsp),
            "size": args.bsp.stat().st_size,
            "ident": bsp.ident,
            "version": bsp.version,
            "map_revision": bsp.map_revision,
            "lumps": bsp.lumps,
        },
    )

    entities_text = bsp.lump(0).decode("utf-8", errors="replace").rstrip("\0")
    (out / "entities.txt").write_text(entities_text, encoding="utf-8")
    entities = parse_entities(entities_text)
    entity_class_counts = collections.Counter(entity.get("classname", "<unknown>") for entity in entities)
    entity_model_refs = collections.defaultdict(list)
    for entity_id, entity in enumerate(entities):
        model = entity.get("model")
        if model and model.startswith("*") and model[1:].isdigit():
            entity_model_refs[int(model[1:])].append(
                {"entity_id": entity_id, "classname": entity.get("classname"), "targetname": entity.get("targetname")}
            )
    write_json(
        out / "entities.json",
        {
            "entity_count": len(entities),
            "classname_counts": dict(entity_class_counts.most_common()),
            "brush_model_references": {str(k): v for k, v in sorted(entity_model_refs.items())},
            "entities": entities,
        },
    )

    planes = parse_planes(bsp)
    vertices = parse_vertices(bsp)
    edges = parse_edges(bsp)
    surfedges = parse_surfedges(bsp)
    texdata, materials = parse_texdata_and_materials(bsp)
    texinfo = parse_texinfo(bsp)
    faces = parse_faces(bsp, 7)
    models = parse_models(bsp)
    brushes, brushsides = parse_brushes_and_sides(bsp, planes, texinfo, texdata)
    dispinfos = parse_dispinfos(bsp)
    dispverts = parse_dispverts(bsp)
    gamelumps = parse_gamelumps(bsp)
    nav_header = parse_nav_header(args.nav) if args.nav else None
    static_prop_lumps = [
        lump.get("static_props", {}) for lump in gamelumps if lump.get("name") == "sprp" and "static_props" in lump
    ]

    face_model_lookup = model_lookup_for_faces(models)
    enriched_faces = []
    material_counts = collections.Counter()
    face_edge_histogram = collections.Counter()
    disp_face_count = 0
    for face in faces:
        vertex_indices = face_vertex_indices(face, edges, surfedges)
        material = material_for_texinfo(face["texinfo"], texinfo, texdata)
        points = [vertices[i] for i in vertex_indices if 0 <= i < len(vertices)]
        model_id = face_model_lookup.get(face["id"])
        if face["dispinfo"] >= 0:
            disp_face_count += 1
        material_counts[material or "__unknown__"] += 1
        face_edge_histogram[len(vertex_indices)] += 1
        enriched = {
            **face,
            "model_id": model_id,
            "material": material,
            "vertex_indices": vertex_indices,
            "bounds": bounds_for_points(points),
        }
        enriched_faces.append(enriched)

    for model in models:
        refs = entity_model_refs.get(model["id"], [])
        model["entity_references"] = refs

    write_json(out / "geometry" / "materials.json", {"texdata": texdata, "texinfo_count": len(texinfo)})
    write_json(out / "geometry" / "models.json", models)
    if static_prop_lumps:
        write_json(out / "geometry" / "static_props.json", static_prop_lumps[0])
    write_json(
        out / "geometry" / "displacements.json",
        {
            "dispinfo_count": len(dispinfos),
            "dispvert_count": len(dispverts),
            "dispinfos": dispinfos,
        },
    )
    with (out / "geometry" / "faces.jsonl").open("w", encoding="utf-8") as f:
        for face in enriched_faces:
            f.write(json.dumps(face, sort_keys=False) + "\n")
    with (out / "geometry" / "brushes.jsonl").open("w", encoding="utf-8") as f:
        for brush in brushes:
            f.write(json.dumps(brush, sort_keys=False) + "\n")

    material_to_mtl = {}
    export_brush_faces_obj(out / "geometry" / "brush_faces.obj", vertices, enriched_faces, material_to_mtl)
    disp_obj_vertices, disp_obj_faces, disp_mesh_records = build_displacement_mesh(
        enriched_faces, dispinfos, dispverts, vertices
    )
    export_displacements_obj(out / "geometry" / "displacements.obj", disp_obj_vertices, disp_obj_faces, material_to_mtl)
    write_materials_mtl(out / "geometry" / "materials.mtl", material_to_mtl)
    write_json(
        out / "geometry" / "displacement_meshes.json",
        {
            "mesh_vertex_count": len(disp_obj_vertices),
            "mesh_triangle_count": sum(len(item["triangles"]) for item in disp_obj_faces),
            "displacements": disp_mesh_records,
        },
    )

    write_json(out / "gamelumps.json", gamelumps)
    if nav_header:
        write_json(out / "nav_header.json", nav_header)

    pak_manifest = None
    if args.pak != "none":
        pak_manifest = unpack_pakfile(bsp, out, args.pak)

    static_prop_summaries = []
    for static_props in static_prop_lumps:
        model_counts = collections.Counter(prop.get("model") or "__unknown__" for prop in static_props.get("props", []))
        static_prop_summaries.append(
            {
                "model_dictionary_count": static_props.get("model_dictionary_count"),
                "prop_count": static_props.get("prop_count"),
                "leaf_count": static_props.get("leaf_count"),
                "inferred_prop_entry_size": static_props.get("inferred_prop_entry_size"),
                "top_models": [
                    {"model": model, "count": count} for model, count in model_counts.most_common(30)
                ],
            }
        )

    summary = {
        "bsp": {
            "ident": bsp.ident,
            "version": bsp.version,
            "map_revision": bsp.map_revision,
            "size": args.bsp.stat().st_size,
        },
        "geometry_counts": {
            "planes": len(planes),
            "vertices": len(vertices),
            "edges": len(edges),
            "surfedges": len(surfedges),
            "faces": len(faces),
            "brush_face_polygons_exported": sum(
                1 for face in enriched_faces if face["dispinfo"] < 0 and len(face["vertex_indices"]) >= 3
            ),
            "displacement_faces": disp_face_count,
            "displacement_infos": len(dispinfos),
            "displacement_vertices": len(dispverts),
            "displacement_mesh_vertices_exported": len(disp_obj_vertices),
            "displacement_mesh_triangles_exported": sum(len(item["triangles"]) for item in disp_obj_faces),
            "brushes": len(brushes),
            "brushsides": len(brushsides),
            "models": len(models),
            "materials": len(texdata),
        },
        "face_edge_histogram": {str(k): v for k, v in sorted(face_edge_histogram.items())},
        "top_materials_by_face_count": [
            {"material": material, "face_count": count} for material, count in material_counts.most_common(30)
        ],
        "entities": {
            "count": len(entities),
            "top_classnames": dict(entity_class_counts.most_common(30)),
            "brush_model_entity_count": sum(len(v) for v in entity_model_refs.values()),
        },
        "static_props": static_prop_summaries,
        "pakfile": (
            {
                "entry_count": pak_manifest.get("entry_count"),
                "counts_by_top_level": pak_manifest.get("counts_by_top_level"),
                "counts_by_extension": pak_manifest.get("counts_by_extension"),
            }
            if pak_manifest
            else None
        ),
        "nav": nav_header,
    }
    write_json(out / "geometry" / "summary.json", summary)
    print(json.dumps(summary["geometry_counts"], indent=2))


if __name__ == "__main__":
    main()
