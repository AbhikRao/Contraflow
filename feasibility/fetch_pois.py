"""Fetch real OSM hospitals (candidate assistance points) and named places (zone anchors)."""
import json, osmnx as ox
BBOX = (80.215, 12.995, 80.275, 13.035)
out = {}
for key, tags in (("hospitals", {"amenity": ["hospital"]}),
                  ("places", {"place": ["suburb", "neighbourhood", "quarter"]})):
    gdf = ox.features_from_bbox(BBOX, tags)
    rows = []
    for idx, r in gdf.iterrows():
        c = r.geometry.centroid
        rows.append({"osm": f"{idx[0]}/{idx[1]}", "name": r.get("name") if isinstance(r.get("name"), str) else None,
                     "lon": c.x, "lat": c.y})
    out[key] = [r for r in rows if r["name"]]
json.dump(out, open("data/osm/adyar_pois.json", "w"), indent=1)
print({k: len(v) for k, v in out.items()}); print([p["name"] for p in out["places"]]); print([p["name"] for p in out["hospitals"]][:40])
