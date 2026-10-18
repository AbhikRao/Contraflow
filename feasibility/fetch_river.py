"""Fetch the Adyar river geometry (OSM waterway) to identify crossings objectively."""
import json, osmnx as ox
from shapely.geometry import mapping
from shapely.ops import unary_union
BBOX = (80.215, 12.995, 80.275, 13.035)
gdf = ox.features_from_bbox(BBOX, {"waterway": ["river"], "natural": ["water"], "water": ["river"]})
names = gdf["name"].fillna("").astype(str) if "name" in gdf else None
sel = gdf[names.str.contains("Adyar|Adayar|அடையாறு", case=False, regex=True)]
geom = unary_union(list(sel.geometry))
json.dump({"features": len(sel), "names": sorted(set(sel["name"].dropna())), "geometry": mapping(geom)},
          open("data/osm/adyar_river.json", "w"))
print(len(sel), sorted(set(sel["name"].dropna())), geom.geom_type)
