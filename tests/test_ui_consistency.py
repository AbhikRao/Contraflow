"""UI/backend consistency: numbers rendered in the browser equal the API's numbers for each preset.
Needs the app served (CF_URL, default http://127.0.0.1:8000) and Google Chrome; skipped otherwise."""
import os
import time

import httpx
import pytest

URL = os.environ.get("CF_URL", "http://127.0.0.1:8000")
playwright = pytest.importorskip("playwright.sync_api")
from ui_nav import go_view, open_analysis, preset, show_agent, use_rules  # noqa: E402


def _up():
    """Ready only if the API is healthy AND the built frontend is served (root 200), not just health."""
    try:
        h = httpx.get(f"{URL}/api/health", timeout=2).json()
        return h.get("frontend_built", True) and httpx.get(URL, timeout=2).status_code == 200
    except Exception:
        return False


@pytest.mark.skipif(not _up(), reason="built app not running at CF_URL (API health and root page both required)")
def test_headline_matches_api_for_presets():
    meta = httpx.get(f"{URL}/api/meta").json()
    bridge = {x["name"]: [c["id"] for c in x["carriageways"]] for x in meta["crossings"]}
    presets = {"Thiru Vi Ka closed": bridge["Thiru Vi Ka Bridge"],
               "Maraimalai Adigal closed": bridge["Maraimalai Adigal Bridge"],
               "All river crossings closed": [c for v in bridge.values() for c in v]}
    with playwright.sync_playwright() as p:
        b = p.chromium.launch(channel="chrome", headless=True)
        pg = b.new_page(viewport={"width": 1512, "height": 900})
        pg.goto(URL, wait_until="load")
        pg.wait_for_selector(".headline .num")
        for label, closed in presets.items():
            preset(pg, label)
            api = httpx.post(f"{URL}/api/evaluate", json={"rule": "B", "closed_carriageways": closed}).json()["metrics"]
            d = api["total_delta_m"]
            want = "0.0 km" if abs(d) < 50 else f"{'+' if d >= 0 else '−'}{abs(d) / 1000:.1f} km"
            for _ in range(40):
                if pg.inner_text(".headline .num").strip() == want:
                    break
                time.sleep(0.1)
            assert pg.inner_text(".headline .num").strip() == want, label
            open_analysis(pg)                                    # zone counts live in the analysis drawer
            stats = pg.inner_text(".stats")
            assert str(api["affected_zones"]) in stats and str(len(api["cut_off"])) in stats
        b.close()
