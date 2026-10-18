"""Rebuild data/processed/network.json from the raw OSM files in data/osm/ (requires osmnx)."""
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from contraflow.model import PROCESSED, build_from_osm  # noqa: E402

net = build_from_osm()
PROCESSED.parent.mkdir(parents=True, exist_ok=True)
PROCESSED.write_text(json.dumps(net.__dict__, separators=(",", ":")))
print(f"wrote {PROCESSED} ({PROCESSED.stat().st_size // 1024} KB): {len(net.segments)} segments, {len(net.zones)} zones")
