"""The processed model used at runtime must equal a fresh build from the raw OSM files."""
import json

import pytest

from contraflow.model import build, build_from_osm

osmnx = pytest.importorskip("osmnx")


def test_processed_model_matches_osm_build():
    a, b = build(), build_from_osm()
    for k in ("junctions", "segments", "zones", "excluded_zones", "help_points", "crossings", "river"):
        assert json.loads(json.dumps(getattr(a, k))) == json.loads(json.dumps(getattr(b, k))), k
