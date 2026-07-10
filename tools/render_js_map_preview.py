#!/usr/bin/env python3
"""Render a top-down SVG preview for a JS-clone map JSON file."""

from __future__ import annotations

import argparse
import colorsys
import html
import json
from pathlib import Path


COLORS = {
    "floor": "#8ba0b4",
    "wall": "#0d6b54",
    "crate": "#2f8f4e",
    "pillar": "#41a064",
    "ramp": "#6d56a8",
    "bombsite": "#e04b3f",
    "t": "#f28c28",
    "ct": "#3d8bff",
    "surfStart": "#111111",
}


def css_color(value, fallback):
    if isinstance(value, int):
        return f"#{value & 0xFFFFFF:06x}"
    return fallback


def solid_is_roof_like(solid, max_thickness=48.0, min_footprint=1.0):
    if solid.get("type") != "wall":
        return False
    dx = solid["max"][0] - solid["min"][0]
    dy = solid["max"][1] - solid["min"][1]
    dz = solid["max"][2] - solid["min"][2]
    return 0 < dy <= max_thickness and dx >= min_footprint and dz >= min_footprint


def collect_bounds(map_data):
    xs = []
    zs = []
    for solid in map_data.get("solids", []):
        xs.extend([solid["min"][0], solid["max"][0]])
        zs.extend([solid["min"][2], solid["max"][2]])
    for ramp in map_data.get("ramps", []):
        xs.extend([ramp["min"][0], ramp["max"][0]])
        zs.extend([ramp["min"][2], ramp["max"][2]])
    for team_spawns in map_data.get("spawns", {}).values():
        for spawn in team_spawns:
            xs.append(spawn[0])
            zs.append(spawn[1])
    for site in map_data.get("bombsites", []):
        xs.extend([site["min"][0], site["max"][0]])
        zs.extend([site["min"][1], site["max"][1]])
    if not xs or not zs:
        return [-512, -512, 512, 512]
    return [min(xs), min(zs), max(xs), max(zs)]


def svg_rect(x, z, width, height, fill, stroke="none", opacity=1.0, stroke_width=1):
    return (
        f'<rect x="{x:.3f}" y="{z:.3f}" width="{width:.3f}" height="{height:.3f}" '
        f'fill="{fill}" stroke="{stroke}" stroke-width="{stroke_width}" opacity="{opacity}"/>'
    )


def color_from_height(value, min_y, max_y):
    if max_y <= min_y:
        t = 0.5
    else:
        t = max(0.0, min(1.0, (value - min_y) / (max_y - min_y)))
    hue = 0.62 - t * 0.45
    red, green, blue = colorsys.hsv_to_rgb(hue, 0.58, 0.92)
    return f"#{int(red * 255):02x}{int(green * 255):02x}{int(blue * 255):02x}"


def render_svg(map_data, width, height, padding, color_by_height=False, show_roof=False):
    min_x, min_z, max_x, max_z = collect_bounds(map_data)
    map_w = max(max_x - min_x, 1)
    map_h = max(max_z - min_z, 1)
    scale = min((width - padding * 2) / map_w, (height - padding * 2) / map_h)
    draw_w = map_w * scale
    draw_h = map_h * scale
    offset_x = (width - draw_w) * 0.5
    offset_z = (height - draw_h) * 0.5

    def tx(x):
        return offset_x + (x - min_x) * scale

    def tz(z):
        return offset_z + (z - min_z) * scale

    def tw(value):
        return value * scale

    body = [
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{width}" height="{height}" viewBox="0 0 {width} {height}">',
        '<rect width="100%" height="100%" fill="#10243a"/>',
    ]
    height_values = [solid["max"][1] for solid in map_data.get("solids", []) if solid.get("type") == "floor"]
    height_values.extend(ramp.get("yMin", ramp["min"][1]) for ramp in map_data.get("ramps", []))
    height_values.extend(ramp.get("yMax", ramp["max"][1]) for ramp in map_data.get("ramps", []))
    min_y = min(height_values) if height_values else 0
    max_y = max(height_values) if height_values else 1

    type_order = {"floor": 0, "ramp": 1, "wall": 2, "crate": 3, "pillar": 4, "roof": 5}
    solids = sorted(
        map_data.get("solids", []),
        key=lambda item: type_order.get("roof" if solid_is_roof_like(item) else item.get("type"), 9),
    )
    for solid in solids:
        is_roof = solid_is_roof_like(solid)
        if is_roof and not show_roof:
            continue
        solid_type = "roof" if is_roof else solid.get("type", "crate")
        x0, z0 = tx(solid["min"][0]), tz(solid["min"][2])
        x1, z1 = tx(solid["max"][0]), tz(solid["max"][2])
        opacity = 0.18 if solid_type == "roof" else 0.72 if solid_type == "floor" else 0.9
        fill = css_color(solid.get("color"), COLORS.get(solid_type, "#777"))
        if color_by_height and solid_type == "floor":
            fill = color_from_height(solid["max"][1], min_y, max_y)
        stroke = "#f4f1dd" if solid_type == "roof" else "none"
        body.append(svg_rect(x0, z0, x1 - x0, z1 - z0, fill, stroke=stroke, opacity=opacity))

    for ramp in map_data.get("ramps", []):
        x0, z0 = tx(ramp["min"][0]), tz(ramp["min"][2])
        x1, z1 = tx(ramp["max"][0]), tz(ramp["max"][2])
        fill = color_from_height((ramp.get("yMin", 0) + ramp.get("yMax", 0)) * 0.5, min_y, max_y) if color_by_height else css_color(ramp.get("color"), COLORS["ramp"])
        body.append(svg_rect(x0, z0, x1 - x0, z1 - z0, fill, stroke=COLORS["ramp"], opacity=0.78, stroke_width=1))

    for site in map_data.get("bombsites", []):
        x0, z0 = tx(site["min"][0]), tz(site["min"][1])
        x1, z1 = tx(site["max"][0]), tz(site["max"][1])
        body.append(svg_rect(x0, z0, x1 - x0, z1 - z0, "none", stroke=COLORS["bombsite"], opacity=1.0, stroke_width=2))
        body.append(
            f'<text x="{x0 + 4:.3f}" y="{z0 + 14:.3f}" fill="{COLORS["bombsite"]}" '
            f'font-family="monospace" font-size="13">{html.escape(str(site.get("name", "?")))}</text>'
        )

    for team, color in (("t", COLORS["t"]), ("ct", COLORS["ct"])):
        for spawn in map_data.get("spawns", {}).get(team, []):
            body.append(f'<circle cx="{tx(spawn[0]):.3f}" cy="{tz(spawn[1]):.3f}" r="5" fill="{color}" stroke="#111" stroke-width="1"/>')

    surf_start = map_data.get("surfStart", [])
    if len(surf_start) >= 3:
        body.append(
            f'<circle cx="{tx(surf_start[0]):.3f}" cy="{tz(surf_start[2]):.3f}" r="8" '
            f'fill="none" stroke="{COLORS["surfStart"]}" stroke-width="2"/>'
        )

    legend_x = 16
    legend_y = 24
    legend_items = [("floor", "floor"), ("wall", "wall"), ("crate", "crate"), ("pillar", "pillar"), ("T", "t"), ("CT", "ct")]
    if show_roof:
        legend_items.insert(2, ("roof", "wall"))
    body.append('<g font-family="monospace" font-size="13">')
    for index, (label, key) in enumerate(legend_items):
        y = legend_y + index * 20
        body.append(f'<rect x="{legend_x}" y="{y - 11}" width="12" height="12" fill="{COLORS[key]}"/>')
        body.append(f'<text x="{legend_x + 18}" y="{y}" fill="#eef3f8">{label}</text>')
    body.append("</g>")
    body.append("</svg>")
    return "\n".join(body) + "\n"


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("map", type=Path)
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--width", type=int, default=1600)
    parser.add_argument("--height", type=int, default=1200)
    parser.add_argument("--padding", type=int, default=48)
    parser.add_argument("--color-by-height", action="store_true")
    parser.add_argument("--show-roof", action="store_true")
    args = parser.parse_args()

    map_data = json.loads(args.map.read_text(encoding="utf-8"))
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(
        render_svg(map_data, args.width, args.height, args.padding, args.color_by_height, args.show_roof),
        encoding="utf-8",
    )
    print(args.out)


if __name__ == "__main__":
    main()
