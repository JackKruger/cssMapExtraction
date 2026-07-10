#!/usr/bin/env python3
"""Render an interactive HTML preview for a JS-clone map JSON file."""

from __future__ import annotations

import argparse
import json
from pathlib import Path


HTML_TEMPLATE = """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{title}</title>
<style>
  html, body {{ margin: 0; height: 100%; background: #101927; color: #e8eef5; font: 13px system-ui, sans-serif; }}
  #toolbar {{ position: fixed; left: 12px; top: 12px; z-index: 2; display: flex; gap: 10px; align-items: center; flex-wrap: wrap; background: rgba(10,16,26,.92); border: 1px solid #2c3c50; padding: 8px 10px; border-radius: 6px; }}
  label {{ display: inline-flex; gap: 5px; align-items: center; white-space: nowrap; }}
  button {{ background: #223349; color: #e8eef5; border: 1px solid #3a506b; border-radius: 5px; padding: 4px 8px; }}
  canvas {{ display: block; width: 100vw; height: 100vh; cursor: grab; }}
  canvas.dragging {{ cursor: grabbing; }}
  #stats {{ position: fixed; right: 12px; top: 12px; z-index: 2; background: rgba(10,16,26,.92); border: 1px solid #2c3c50; padding: 8px 10px; border-radius: 6px; white-space: pre; }}
</style>
</head>
<body>
<div id="toolbar">
  <strong>{name}</strong>
  <label><input type="checkbox" data-layer="floor" checked> floors</label>
  <label><input type="checkbox" data-layer="wall" checked> walls</label>
  <label><input type="checkbox" data-layer="roof"> roof</label>
  <label><input type="checkbox" data-layer="asset" checked> assets</label>
  <label><input type="checkbox" data-layer="spawn" checked> spawns</label>
  <label><input type="checkbox" data-layer="site" checked> sites</label>
  <button id="fit">Fit</button>
</div>
<div id="stats"></div>
<canvas id="map"></canvas>
<script>
const mapData = {map_json};
const canvas = document.getElementById('map');
const ctx = canvas.getContext('2d');
const stats = document.getElementById('stats');
const layers = {{ floor: true, wall: true, roof: false, asset: true, spawn: true, site: true }};
let scale = 1;
let offsetX = 0;
let offsetY = 0;
let dragging = false;
let lastX = 0;
let lastY = 0;

const colors = {{
  floor: '#8ba0b4',
  wall: '#0d6b54',
  crate: '#2f8f4e',
  pillar: '#41a064',
  ramp: '#6d56a8',
  t: '#f28c28',
  ct: '#3d8bff',
  site: '#e04b3f'
}};

function resize() {{
  const dpr = window.devicePixelRatio || 1;
  canvas.width = Math.floor(innerWidth * dpr);
  canvas.height = Math.floor(innerHeight * dpr);
  ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
  draw();
}}

function bounds() {{
  const xs = [], zs = [];
  for (const s of mapData.solids || []) {{ xs.push(s.min[0], s.max[0]); zs.push(s.min[2], s.max[2]); }}
  for (const r of mapData.ramps || []) {{ xs.push(r.min[0], r.max[0]); zs.push(r.min[2], r.max[2]); }}
  for (const team of Object.values(mapData.spawns || {{}})) for (const p of team) {{ xs.push(p[0]); zs.push(p[2]); }}
  for (const site of mapData.bombsites || []) {{ xs.push(site.min[0], site.max[0]); zs.push(site.min[1], site.max[1]); }}
  return [Math.min(...xs), Math.min(...zs), Math.max(...xs), Math.max(...zs)];
}}

function fit() {{
  const [minX, minZ, maxX, maxZ] = bounds();
  const pad = 48;
  scale = Math.min((innerWidth - pad * 2) / (maxX - minX || 1), (innerHeight - pad * 2) / (maxZ - minZ || 1));
  offsetX = (innerWidth - (maxX - minX) * scale) / 2 - minX * scale;
  offsetY = (innerHeight - (maxZ - minZ) * scale) / 2 - minZ * scale;
  draw();
}}

function sx(x) {{ return x * scale + offsetX; }}
function sy(z) {{ return z * scale + offsetY; }}

function color(value, fallback) {{
  return Number.isInteger(value) ? `#${{(value & 0xffffff).toString(16).padStart(6, '0')}}` : fallback;
}}

function isRoof(s) {{
  if (s.type !== 'wall') return false;
  const dx = s.max[0] - s.min[0];
  const dy = s.max[1] - s.min[1];
  const dz = s.max[2] - s.min[2];
  return dy > 0 && dy <= 48 && dx >= 1 && dz >= 1;
}}

function rect(minX, minZ, maxX, maxZ, fill, alpha = 1, stroke = null) {{
  const x = sx(minX), y = sy(minZ), w = (maxX - minX) * scale, h = (maxZ - minZ) * scale;
  ctx.globalAlpha = alpha;
  if (fill) {{ ctx.fillStyle = fill; ctx.fillRect(x, y, w, h); }}
  if (stroke) {{ ctx.strokeStyle = stroke; ctx.lineWidth = 2; ctx.strokeRect(x, y, w, h); }}
  ctx.globalAlpha = 1;
}}

function draw() {{
  ctx.clearRect(0, 0, innerWidth, innerHeight);
  ctx.fillStyle = '#10243a';
  ctx.fillRect(0, 0, innerWidth, innerHeight);
  for (const s of mapData.solids || []) {{
    if (s.type === 'floor' && layers.floor) rect(s.min[0], s.min[2], s.max[0], s.max[2], color(s.color, colors.floor), .72);
  }}
  for (const s of mapData.solids || []) {{
    if (s.type === 'wall' && !isRoof(s) && layers.wall) rect(s.min[0], s.min[2], s.max[0], s.max[2], color(s.color, colors.wall), .9);
  }}
  for (const r of mapData.ramps || []) {{
    if (layers.floor) rect(r.min[0], r.min[2], r.max[0], r.max[2], color(r.color, colors.ramp), .78, colors.ramp);
  }}
  for (const s of mapData.solids || []) {{
    if (isRoof(s) && layers.roof) rect(s.min[0], s.min[2], s.max[0], s.max[2], color(s.color, colors.wall), .18, '#f4f1dd');
  }}
  for (const s of mapData.solids || []) {{
    if ((s.type === 'crate' || s.type === 'pillar') && layers.asset) rect(s.min[0], s.min[2], s.max[0], s.max[2], color(s.color, colors[s.type]), .9);
  }}
  if (layers.site) {{
    for (const site of mapData.bombsites || []) {{
      rect(site.min[0], site.min[1], site.max[0], site.max[1], null, 1, colors.site);
      ctx.fillStyle = colors.site;
      ctx.fillText(site.name || '?', sx(site.min[0]) + 5, sy(site.min[1]) + 15);
    }}
  }}
  if (layers.spawn) {{
    for (const [team, points] of Object.entries(mapData.spawns || {{}})) {{
      ctx.fillStyle = colors[team] || '#fff';
      for (const p of points) {{
        ctx.beginPath();
        ctx.arc(sx(p[0]), sy(p[2]), 5, 0, Math.PI * 2);
        ctx.fill();
      }}
    }}
  }}
  const counts = (mapData.solids || []).reduce((acc, s) => {{
    const key = isRoof(s) ? 'roof' : s.type;
    acc[key] = (acc[key] || 0) + 1;
    return acc;
  }}, {{}});
  stats.textContent = `solids: ${{(mapData.solids || []).length}}\\nfloor: ${{counts.floor || 0}}\\nwall: ${{counts.wall || 0}}\\nroof: ${{counts.roof || 0}}\\ncrate: ${{counts.crate || 0}}\\npillar: ${{counts.pillar || 0}}\\nramps: ${{(mapData.ramps || []).length}}`;
}}

document.querySelectorAll('[data-layer]').forEach(input => input.addEventListener('change', e => {{
  layers[e.target.dataset.layer] = e.target.checked;
  draw();
}}));
document.getElementById('fit').addEventListener('click', fit);
canvas.addEventListener('mousedown', e => {{ dragging = true; lastX = e.clientX; lastY = e.clientY; canvas.classList.add('dragging'); }});
addEventListener('mouseup', () => {{ dragging = false; canvas.classList.remove('dragging'); }});
addEventListener('mousemove', e => {{
  if (!dragging) return;
  offsetX += e.clientX - lastX;
  offsetY += e.clientY - lastY;
  lastX = e.clientX;
  lastY = e.clientY;
  draw();
}});
canvas.addEventListener('wheel', e => {{
  e.preventDefault();
  const factor = e.deltaY < 0 ? 1.1 : 0.9;
  const mx = e.clientX, my = e.clientY;
  offsetX = mx - (mx - offsetX) * factor;
  offsetY = my - (my - offsetY) * factor;
  scale *= factor;
  draw();
}}, {{ passive: false }});
addEventListener('resize', resize);
resize();
fit();
</script>
</body>
</html>
"""


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("map", type=Path)
    parser.add_argument("--out", type=Path, required=True)
    args = parser.parse_args()

    map_data = json.loads(args.map.read_text(encoding="utf-8"))
    args.out.parent.mkdir(parents=True, exist_ok=True)
    html = HTML_TEMPLATE.format(
        title=map_data.get("title", "Map Preview"),
        name=map_data.get("name", args.map.stem),
        map_json=json.dumps(map_data, separators=(",", ":")),
    )
    args.out.write_text(html, encoding="utf-8")
    print(args.out)


if __name__ == "__main__":
    main()
