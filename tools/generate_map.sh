#!/usr/bin/env bash
# Generate the canonical and grid-resolution map variants.
#
# Usage: tools/generate_map.sh [--all] [--grid-sizes 64,96,128] [map-name ...]
#   --all                    generate every source/<map>/ directory with a .bsp
#   --grid-sizes SIZES       comma-separated nav cell sizes for the grid matrix
#                             (default: 64,96,128)
#
# With no map names, --all is implied. Every map gets the six canonical variants
# plus flat/layered clean and asset variants for each requested grid size. Roof
# slabs are deliberately omitted: nav data cannot identify enclosed regions.
#
# Run from the repo root.
set -euo pipefail

usage() {
  printf '%s\n' \
    'usage: tools/generate_map.sh [--all] [--grid-sizes 64,96,128] [map-name ...]' \
    '  --all                    generate every source map containing a .bsp' \
    '  --grid-sizes SIZES       comma-separated positive nav cell sizes' \
    '  --help                   show this message'
}

grid_sizes_raw="${GRID_SIZES:-64,96,128}"
all_maps=false
declare -a maps=()

while (($#)); do
  case "$1" in
    --all)
      all_maps=true
      ;;
    --grid-sizes)
      shift
      (($#)) || { echo '--grid-sizes requires a value' >&2; exit 2; }
      grid_sizes_raw="$1"
      ;;
    --help|-h)
      usage
      exit 0
      ;;
    --*)
      echo "unknown option: $1" >&2
      usage >&2
      exit 2
      ;;
    *)
      maps+=("$1")
      ;;
  esac
  shift
done

declare -a grid_sizes=()
declare -A seen_grid_sizes=()
IFS=',' read -r -a requested_grid_sizes <<< "$grid_sizes_raw"
for grid_size in "${requested_grid_sizes[@]}"; do
  grid_size="${grid_size//[[:space:]]/}"
  [[ "$grid_size" =~ ^[1-9][0-9]*$ ]] || {
    echo "invalid grid size: $grid_size" >&2
    exit 2
  }
  if [[ -z "${seen_grid_sizes[$grid_size]+x}" ]]; then
    grid_sizes+=("$grid_size")
    seen_grid_sizes[$grid_size]=1
  fi
done
(( ${#grid_sizes[@]} )) || { echo 'at least one grid size is required' >&2; exit 2; }

if "$all_maps" || (( ${#maps[@]} == 0 )); then
  shopt -s nullglob
  for src_dir in source/*; do
    [[ -d "$src_dir" ]] || continue
    bsp_files=("$src_dir"/*.bsp)
    (( ${#bsp_files[@]} )) || continue
    maps+=("${src_dir#source/}")
  done
  shopt -u nullglob
fi
(( ${#maps[@]} )) || { echo 'no source maps with a .bsp were found' >&2; exit 1; }

map_title() {
  case "$1" in
    de_mirage_csgo) printf '%s' 'De Mirage CS:GO' ;;
    de_dust2) printf '%s' 'De Dust2' ;;
    de_dust2_winter) printf '%s' 'De Dust2 Winter' ;;
    *) printf '%s' "${1//_/ }" ;;
  esac
}

map_engine_theme() {
  case "$1" in
    de_dust2_winter) printf '%s' ice ;;
    *) printf '%s' sand ;;
  esac
}

render_variant() {
  local map="$1"
  local out="$2"
  local variant="$3"
  local stem="${map}_${variant}"
  local json="$out/$variant/$stem.json"

  python3 tools/render_js_map_preview.py "$json" --out "$out/$variant/$stem.svg" >/dev/null
  python3 tools/render_js_map_html.py "$json" --out "$out/$variant/$stem.html" >/dev/null
}

generate_flat_variant() {
  local map="$1"
  local title="$2"
  local bsp="$3"
  local extracted="$4"
  local nav="$5"
  local out="$6"
  local variant="$7"
  local grid_size="$8"
  local with_assets="$9"
  local mode="${10}"
  local theme
  theme="$(map_engine_theme "$map")"
  local -a args=(
    python3 tools/convert_bsp_to_js_map_flat.py "$bsp" --extracted "$extracted"
    --nav "$nav" --out "$out/$variant/${map}_${variant}.json"
    --name "${map}_${variant}" --title "$title" --theme "$theme"
    --nav-floor-mode "$mode"
  )

  if [[ "$mode" == grid ]]; then
    args+=(--nav-cell-size "$grid_size")
  fi
  if [[ "$with_assets" == true ]]; then
    args+=(--include-assets)
  fi
  "${args[@]}" >/dev/null
  render_variant "$map" "$out" "$variant"
}

generate_layered_variant() {
  local map="$1"
  local title="$2"
  local bsp="$3"
  local extracted="$4"
  local nav="$5"
  local out="$6"
  local variant="$7"
  local grid_size="$8"
  local with_assets="$9"
  local theme
  theme="$(map_engine_theme "$map")"
  local -a args=(
    python3 tools/convert_nav_to_layered_js_map.py "$bsp" --extracted "$extracted"
    --nav "$nav" --out "$out/$variant/${map}_${variant}.json"
    --name "${map}_${variant}" --title "$title" --theme "$theme"
    --nav-cell-size "$grid_size"
  )

  if [[ "$with_assets" == true ]]; then
    args+=(--include-assets)
  fi
  "${args[@]}" >/dev/null
  render_variant "$map" "$out" "$variant"
}

generate_map() {
  local map="$1"
  local src="source/$map"
  local out="build/$map"
  local extracted="$out/extracted"
  local title
  local -a bsp_files nav_files navflag

  [[ -d "$src" ]] || { echo "source directory not found: $src" >&2; return 1; }
  shopt -s nullglob
  bsp_files=("$src"/*.bsp)
  nav_files=("$src"/*.nav)
  shopt -u nullglob
  (( ${#bsp_files[@]} )) || { echo "no .bsp under $src" >&2; return 1; }
  (( ${#bsp_files[@]} == 1 )) || { echo "multiple .bsp files under $src" >&2; return 1; }
  (( ${#nav_files[@]} )) || { echo "no .nav under $src; all grid and layered variants require one" >&2; return 1; }
  (( ${#nav_files[@]} == 1 )) || { echo "multiple .nav files under $src" >&2; return 1; }

  local bsp="${bsp_files[0]}"
  local nav="${nav_files[0]}"
  title="$(map_title "$map")"
  navflag=(--nav "$nav")

  echo ">> $map: extracting"
  if [[ -f "$extracted/entities.json" && "${FORCE_EXTRACT:-0}" != 1 ]]; then
    echo "   reusing existing extraction ($extracted); set FORCE_EXTRACT=1 to redo"
  else
    python3 tools/extract_bsp_geometry.py "$bsp" "${navflag[@]}" --out "$extracted" --pak all >/dev/null
  fi

  echo ">> $map: canonical variants"
  python3 tools/convert_bsp_to_js_map.py "$bsp" --extracted "$extracted" \
    --out "$out/detailed/${map}_detailed.json" --name "${map}_detailed" --title "$title Detailed" --theme "$(map_engine_theme "$map")" >/dev/null
  render_variant "$map" "$out" detailed

  generate_flat_variant "$map" "$title Flat" "$bsp" "$extracted" "$nav" "$out" flat 128 false grid
  generate_flat_variant "$map" "$title Flat Assets" "$bsp" "$extracted" "$nav" "$out" flat_assets 128 true grid
  generate_flat_variant "$map" "$title Flat Exact" "$bsp" "$extracted" "$nav" "$out" flat_exact 128 false exact
  generate_layered_variant "$map" "$title Layered" "$bsp" "$extracted" "$nav" "$out" layered 128 false
  generate_layered_variant "$map" "$title Layered Assets" "$bsp" "$extracted" "$nav" "$out" layered_assets 128 true

  echo ">> $map: grid variants (${grid_sizes[*]})"
  for grid_size in "${grid_sizes[@]}"; do
    generate_flat_variant "$map" "$title Flat Grid ${grid_size}" "$bsp" "$extracted" "$nav" "$out" "flat_grid_$grid_size" "$grid_size" false grid
    generate_flat_variant "$map" "$title Flat Assets Grid ${grid_size}" "$bsp" "$extracted" "$nav" "$out" "flat_assets_grid_$grid_size" "$grid_size" true grid
    generate_layered_variant "$map" "$title Layered Grid ${grid_size}" "$bsp" "$extracted" "$nav" "$out" "layered_grid_$grid_size" "$grid_size" false
    generate_layered_variant "$map" "$title Layered Assets Grid ${grid_size}" "$bsp" "$extracted" "$nav" "$out" "layered_assets_grid_$grid_size" "$grid_size" true
  done

  echo ">> done: $out"
}

declare -A seen_maps=()
for map in "${maps[@]}"; do
  if [[ -n "${seen_maps[$map]+x}" ]]; then
    continue
  fi
  seen_maps[$map]=1
  generate_map "$map"
done
