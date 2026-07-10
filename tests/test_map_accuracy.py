#!/usr/bin/env python3
"""Accuracy regression tests for the generated de_mirage maps.

Run: ``python3 -m unittest discover -s tests`` from the repo root.

These run against the version-controlled ``.nav`` + extracted ``entities.json``
and the committed map JSON, so they do NOT need the large (gitignored) ``.bsp``.

Two kinds of assertion:

* Invariants that must always hold (no walkable area lost, positions exact,
  flat maps flat, layered maps vertical, flat map fully connected).
* Documented baselines for known accuracy gaps (invented floor, layered
  connectivity). These are ceilings/floors that fail if accuracy *regresses*.
  The TARGET column notes where we want them; see ``TestPlayableTarget`` for
  the config that reaches the target. Tighten the baseline when the defaults
  are retuned.
"""

from __future__ import annotations

import json
from pathlib import Path
import sys
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))

from convert_bsp_to_js_map_flat import (  # noqa: E402
    cells_from_rects,
    load_entities,
    parse_nav_area_rects,
    parse_vec3,
    parse_yaw,
)
from convert_nav_to_layered_js_map import close_narrow_wall_gaps, usable_stair_steps  # noqa: E402
from measure_map_accuracy import measure, raster_rects  # noqa: E402
from validate_js_map import validate_map  # noqa: E402

NAV = ROOT / "source/de_mirage_csgo/de_mirage_csgo.nav"
ENTITIES = ROOT / "build/de_mirage_csgo/extracted/entities.json"
BSP = ROOT / "source/de_mirage_csgo/de_mirage_csgo.bsp"


def map_path(variant):
    return ROOT / f"build/de_mirage_csgo/{variant}/de_mirage_csgo_{variant}.json"


def report(variant):
    return measure(map_path(variant), NAV, ENTITIES, fine=8, cell=64, flat=True)


def load(variant):
    return json.loads(map_path(variant).read_text(encoding="utf-8"))


def metadata(variant):
    return json.loads(map_path(variant).with_suffix(".meta.json").read_text(encoding="utf-8"))


def canonical_map_path(map_name, variant):
    return ROOT / f"build/{map_name}/{variant}/{map_name}_{variant}.json"


# Current measured values (baseline). TARGET is the playable goal.
#   flat_assets    invented +51.96%   (target < 35%)
#   layered_assets invented +51.09%   (target < 35%)
#   layered_assets connectivity 31/33 (target 33/33)
FLAT_INVENTED_CEILING = 52.5
LAYERED_INVENTED_CEILING = 52.0
LAYERED_ANCHOR_FLOOR = 31
ENGINE_RUNTIME_FIELDS = {
    "skyTop", "skyHorizon", "skyColor", "fogColor", "fogNear", "fogFar",
    "sunDir", "sunColor", "sunIntensity", "hemiSky", "hemiGround",
    "hemiIntensity", "bounds", "buyzones", "nav",
}


class TestInvariants(unittest.TestCase):
    """Properties that must never break."""

    def test_no_walkable_area_lost(self):
        for variant in ("flat_assets", "layered_assets"):
            with self.subTest(variant=variant):
                self.assertLess(report(variant)["footprint"]["lost_floor_pct"], 1.0)

    def test_spawn_and_site_positions_exact(self):
        for variant in ("flat_assets", "layered_assets"):
            with self.subTest(variant=variant):
                self.assertTrue(report(variant)["positions"]["all_exact"])

    def test_flat_map_is_flat(self):
        self.assertEqual(report("flat_assets")["distinct_floor_heights"], 1)

    def test_layered_map_preserves_verticality(self):
        self.assertGreaterEqual(report("layered_assets")["distinct_floor_heights"], 5)

    def test_flat_map_fully_connected(self):
        data = load("flat_assets")
        result = validate_map(data, wall_clearance=16.0, asset_clearance=48.0,
                              require_spawn_yaws=False, check_connectivity=True,
                              connectivity_grid_size=64.0, player_radius=24.0)
        conn = result["connectivity"]
        self.assertEqual(conn["anchors_reached"], conn["anchors"])


class TestAccuracyBaselines(unittest.TestCase):
    """Known accuracy gaps guarded against regression."""

    def test_flat_invented_floor_within_baseline(self):
        self.assertLessEqual(report("flat_assets")["footprint"]["invented_floor_pct"], FLAT_INVENTED_CEILING)

    def test_layered_invented_floor_within_baseline(self):
        self.assertLessEqual(report("layered_assets")["footprint"]["invented_floor_pct"], LAYERED_INVENTED_CEILING)

    def test_layered_connectivity_not_worse(self):
        data = load("layered_assets")
        result = validate_map(data, wall_clearance=16.0, asset_clearance=48.0,
                              require_spawn_yaws=False, check_connectivity=True,
                              connectivity_grid_size=64.0, player_radius=24.0)
        self.assertGreaterEqual(result["connectivity"]["anchors_reached"], LAYERED_ANCHOR_FLOOR)


class TestEngineMapContract(unittest.TestCase):
    """Fields required when a generated JSON map is loaded directly by the game."""

    def test_runtime_fields_are_present(self):
        data = load("flat_assets")
        self.assertTrue(ENGINE_RUNTIME_FIELDS.issubset(data))
        self.assertIn(data["theme"], {"sand", "ice", "industrial"})
        self.assertEqual(set(data["buyzones"]), {"t", "ct"})
        self.assertEqual(len(data["bounds"]["min"]), 3)
        self.assertEqual(len(data["bounds"]["max"]), 3)

    def test_generated_ramps_are_walkable(self):
        data = load("detailed")
        self.assertTrue(data["ramps"])
        self.assertTrue(all(ramp.get("walk") is True for ramp in data["ramps"]))

    def test_generated_maps_do_not_emit_pillars(self):
        for variant in ("detailed", "flat_assets", "layered_assets"):
            with self.subTest(variant=variant):
                self.assertFalse(any(solid.get("type") == "pillar" for solid in load(variant)["solids"]))


class TestLayeredEngineGeometry(unittest.TestCase):
    """Layered defaults must match the supplied game movement hull."""

    def test_stair_playability_requires_hull_width_and_step_limit(self):
        short_ramp = {
            "min": [0.0, 0.0, 0.0], "max": [25.0, 32.0, 75.0],
            "axis": 0, "yMin": 0.0, "yMax": 32.0,
        }
        self.assertIsNone(usable_stair_steps(short_ramp, 16.0, 2, 16, 32.0, 18.0))
        short_ramp["max"][0] = 128.0
        self.assertEqual(usable_stair_steps(short_ramp, 16.0, 2, 16, 32.0, 18.0), 2)

    def test_player_width_wall_gaps_are_closed(self):
        wall_spans = [[0.0, 0.0, 128.0, 24.0], [160.0, 0.0, 288.0, 24.0]]
        self.assertEqual(close_narrow_wall_gaps(wall_spans, 32.0), [[0.0, 0.0, 288.0, 24.0]])
        self.assertEqual(len(close_narrow_wall_gaps(wall_spans, 31.0)), 2)

    def test_default_layered_output_has_full_height_containment(self):
        data = load("layered_assets")
        meta = metadata("layered_assets")
        params = meta["parameters"]
        self.assertEqual(params["slope_mode"], "auto")
        self.assertEqual(params["player_radius"], 16.0)
        self.assertEqual(params["player_height"], 72.0)
        self.assertEqual(params["player_step_height"], 18.0)
        self.assertFalse(params["stair_side_walls"])
        self.assertEqual(meta["output_counts"]["stair_side_walls"], 0)
        self.assertGreater(meta["output_counts"]["ramps"], 0)
        self.assertTrue(all(ramp.get("walk") is True for ramp in data["ramps"]))

        floors = [solid for solid in data["solids"] if solid.get("type") == "floor"]
        ring = meta["global_containment"]
        self.assertLessEqual(ring["min_y"], min(solid["min"][1] for solid in floors))
        self.assertGreaterEqual(ring["max_y"], max(solid["max"][1] for solid in floors) + params["wall_height"])

    def test_layered_walls_are_full_height_curtains(self):
        data = load("layered_assets")
        meta = metadata("layered_assets")
        base_y = meta["global_containment"]["structural_wall_base_y"]
        foundation_top_y = meta["global_containment"]["foundation_top_y"]
        walls = [solid for solid in data["solids"] if solid.get("type") == "wall"]
        floors = [solid for solid in data["solids"] if solid.get("type") == "floor"]
        foundation = [solid for solid in floors if solid["min"][1] == base_y and solid["max"][1] == foundation_top_y]
        self.assertTrue(walls)
        self.assertGreater(meta["output_counts"]["foundation_floor_rects"], 0)
        self.assertTrue(foundation)
        self.assertTrue(all(solid["min"][1] <= base_y for solid in walls))
        self.assertTrue(all(solid["max"][1] - solid["min"][1] >= 72.0 for solid in walls))
        for solid in floors:
            if solid["max"][1] <= foundation_top_y:
                continue
            x = (solid["min"][0] + solid["max"][0]) * 0.5
            z = (solid["min"][2] + solid["max"][2]) * 0.5
            self.assertTrue(any(
                base["min"][0] <= x <= base["max"][0] and base["min"][2] <= z <= base["max"][2]
                for base in foundation
            ))


class TestCombatMapSchema(unittest.TestCase):
    """Generated combat JSON must match the supplied game's import contract."""

    def test_spawns_and_bombsites_match_game_schema(self):
        for map_name in ("de_dust2", "de_dust2_winter", "de_mirage_csgo"):
            with self.subTest(map=map_name):
                data = json.loads(canonical_map_path(map_name, "layered").read_text(encoding="utf-8"))
                entities = load_entities(ROOT / f"build/{map_name}/extracted")
                for team, classname in (("t", "info_player_terrorist"), ("ct", "info_player_counterterrorist")):
                    source_spawns = [entity for entity in entities if entity.get("classname") == classname and "origin" in entity]
                    self.assertEqual(len(data["spawns"][team]), len(source_spawns))
                    for spawn, entity in zip(data["spawns"][team], source_spawns):
                        source = parse_vec3(entity["origin"])
                        self.assertEqual(len(spawn), 3)
                        self.assertAlmostEqual(spawn[0], source[0])
                        self.assertAlmostEqual(spawn[1], source[1])
                        self.assertAlmostEqual(spawn[2], parse_yaw(entity))
                self.assertEqual([site["name"] for site in data["bombsites"]], ["A", "B"])


class TestPlayableTarget(unittest.TestCase):
    """The recommended playable grid resolution meets the fidelity target.

    Pure nav-mesh property (exercises the converter's gridding), no BSP needed.
    """

    def test_grid64_invented_floor_below_target(self):
        nav_areas = parse_nav_area_rects(NAV)["areas"]
        rects = [a["rect"] for a in nav_areas]
        truth = raster_rects(rects, 8)
        cells = cells_from_rects(rects, 64)
        floor = raster_rects([[ix * 64, iz * 64, (ix + 1) * 64, (iz + 1) * 64] for ix, iz in cells], 8)
        invented_pct = 100 * len(floor - truth) / len(truth)
        self.assertLess(invented_pct, 35.0)


if __name__ == "__main__":
    unittest.main()
