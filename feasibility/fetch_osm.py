"""Fetch a bounded OSM drive network for Chennai's Adyar corridor (ODbL, (c) OpenStreetMap contributors)."""
import json, sys, time
import osmnx as ox
# (west, south, east, north): Saidapet - Kotturpuram - Adyar - Besant Nagar, both river banks
BBOX = (80.215, 12.995, 80.275, 13.035)
t0 = time.time()
G = ox.graph_from_bbox(BBOX, network_type="drive", simplify=True, retain_all=False, truncate_by_edge=True)
ox.save_graphml(G, "data/osm/adyar_drive.graphml")
meta = {"bbox_w_s_e_n": BBOX, "network_type": "drive", "osmnx": ox.__version__, "fetched_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "nodes": G.number_of_nodes(), "edges": G.number_of_edges(), "seconds": round(time.time()-t0, 1),
        "source": "OpenStreetMap via Overpass API (osmnx)", "license": "ODbL 1.0, (c) OpenStreetMap contributors"}
json.dump(meta, open("data/osm/adyar_drive.meta.json", "w"), indent=1); print(meta)
