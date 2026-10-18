"""Browser checks (Playwright + installed Chrome) for the competitive upgrade: investigation timeline, streamed tool
progress, suggested actions -> proposal -> human apply, critical-roads tab and the memory view.
Requires the built app at CF_URL (default http://127.0.0.1:8000); skipped when not reachable."""
import os
import time

import httpx
import pytest

URL = os.environ.get("CF_URL", "http://127.0.0.1:8000")
sync_api = pytest.importorskip("playwright.sync_api")
from ui_nav import go_view, open_analysis, preset, show_agent, use_rules  # noqa: E402
Q = "Saidapet bridge may be flooded. Which neighbourhoods are affected, keeping Kotturpuram reserved for ambulances?"


def _ready():
    try:
        return httpx.get(f"{URL}/api/health", timeout=2).json().get("frontend_built", True)
    except Exception:
        return False


pytestmark = pytest.mark.skipif(not _ready(), reason="built app not running at CF_URL")


@pytest.fixture()
def page():
    with sync_api.sync_playwright() as p:
        b = p.chromium.launch(channel="chrome", headless=True)
        pg = b.new_page(viewport={"width": 1512, "height": 900})
        pg.goto(URL, wait_until="load")
        pg.wait_for_selector(".headline .num")
        yield pg
        b.close()


def wait_text(pg, sel, pred, timeout=30):
    end = time.time() + timeout
    while time.time() < end:
        try:
            t = pg.inner_text(sel)
            if pred(t):
                return t
        except Exception:
            pass
        time.sleep(0.15)
    raise AssertionError(f"timeout waiting on {sel}: {pg.inner_text(sel) if pg.query_selector(sel) else 'missing'}")


def ask(pg, msg):
    show_agent(pg)
    use_rules(pg)
    # record whether streamed progress was ever rendered (it can be brief)
    pg.evaluate("""() => { window.__sawLive = false; new MutationObserver(() => {
        if (document.querySelector('[data-testid=live-steps] li')) window.__sawLive = true
      }).observe(document.body, {subtree: true, childList: true}) }""")
    n = len(pg.query_selector_all(".a-card"))
    pg.fill(".agent-input textarea", msg)
    pg.keyboard.press("Enter")
    end = time.time() + 60
    while len(pg.query_selector_all(".a-card")) <= n and time.time() < end:
        time.sleep(0.2)
    return pg.query_selector_all(".a-card")[-1]


def test_investigation_shows_pipeline_provenance_impact_comparison_and_changes_nothing(page):
    before = page.inner_text(".headline .num")
    card = ask(page, Q)
    pipe = card.query_selector("[data-testid=pipeline]")
    states = {li.get_attribute("data-stage"): li.get_attribute("data-state") for li in pipe.query_selector_all("li")}
    assert states == {"report": "done", "investigation": "done", "impact": "done", "alternatives": "done",
                      "recommendation": "done", "decision": "attention"}      # stages are earned by completed tools; decision pending
    text = pipe.inner_text()
    assert "Maraimalai Adigal Bridge" in text and "+11.92 km" in text and "awaiting you" in text
    zones = card.query_selector("[data-testid=affected-zones]").inner_text()
    assert "Guindy +3.63 km" in zones
    assert "wins" in card.query_selector("[data-testid=comparison]").inner_text()
    assert "reserve emergency on Kotturpuram" in card.query_selector("[data-testid=assumptions]").inner_text()
    kinds = {t.get_attribute("data-prov") for t in card.query_selector_all("[data-prov]")}
    assert {"graph", "assume"} <= kinds and ("agent" in kinds or "system" in kinds)     # provenance is visible
    assert "report" in {t.get_attribute("data-prov") for t in page.query_selector_all(".u-msg [data-prov]")}
    assert card.query_selector(".prop") is None                          # no proposal from an investigation
    assert page.inner_text(".headline .num") == before                   # scenario unchanged
    assert page.evaluate("window.__sawLive") is True                     # real tool steps were streamed


def test_suggested_action_goes_through_proposal_and_human_apply(page):
    card = ask(page, Q)
    card.query_selector("[data-testid=suggested-actions] button:has-text('Propose closing')").click()
    end = time.time() + 30
    while len(page.query_selector_all(".a-card")) < 2 and time.time() < end:
        time.sleep(0.2)
    prop = page.query_selector_all(".a-card")[-1]
    assert prop.query_selector(".prop button:has-text('Apply')")
    assert page.inner_text(".headline .num").startswith("79.6")          # still nothing applied
    prop.query_selector(".prop button:has-text('Apply')").click()
    wait_text(page, ".headline .num", lambda t: t.startswith("+11.9"))   # added distance, recomputed
    time.sleep(0.5)
    inv = httpx.get(f"{URL}/api/memory").json()["investigations"]
    assert any(p[2] == "applied" and "Maraimalai" in p[1] for i in inv for p in i["proposals"])


def test_critical_roads_tab_recomputes_for_the_scenario(page):
    open_analysis(page, "Road sensitivity")
    page.wait_for_selector("[data-testid=vuln-row]", timeout=30000)
    first = page.query_selector_all("[data-testid=vuln-row]")[0].inner_text()
    assert "unnamed road (residential, 87 m)" in first and "Less obvious road, large modeled effect" in first
    scope = page.inner_text("[data-testid=scope-card]")
    assert "not every road" in scope and "cannot tell you" in scope.lower()          # limits are visible, not hidden
    caveat = page.inner_text("[data-testid=access-caveat]")
    assert "Government Hospital, Adyar" in caveat and "not a finding about this street" in caveat
    page.query_selector_all("[data-testid=vuln-row]")[0].query_selector("button:has-text('Test closure')").click()
    wait_text(page, ".headline .num", lambda t: t.startswith("+44.0"))   # added distance, recomputed by FalkorDB
    # the analysis re-runs for the new scenario: the closed link is no longer a candidate
    end = time.time() + 40
    while time.time() < end:
        rows = page.query_selector_all("[data-testid=vuln-row]")
        if rows and rows[0].inner_text() != first:
            break
        time.sleep(0.3)
    assert rows and rows[0].inner_text() != first


def test_memory_view_lists_investigations(page):
    ask(page, "What if Abraham bridge were closed?")
    go_view(page, "History")
    t = wait_text(page, "[data-testid=memory-view]", lambda t: "What if Abraham bridge were closed?" in t)
    assert "unverified" in t.lower() and "re-evaluat" in t.lower()      # historical results are labelled and re-checked


def test_compare_tab_shows_live_preset_numbers_and_loads_a_scenario(page):
    go_view(page, "Compare")
    page.wait_for_selector("[data-testid=compare-panel]", timeout=30000)
    wait_text(page, "[data-testid=compare-panel]", lambda t: "Maraimalai Adigal" in t and "km" in t)
    # figures must equal an independent API evaluation, never constants in the UI
    meta = httpx.get(f"{URL}/api/meta").json()
    cw = lambda name: [c["id"] for x in meta["crossings"] if x["name"].startswith(name) for c in x["carriageways"]]  # noqa: E731
    ev = lambda closed: httpx.post(f"{URL}/api/evaluate", json={"rule": "B", "closed_carriageways": closed}).json()["metrics"]["total_delta_m"]  # noqa: E731
    mab = ev(cw("Maraimalai Adigal")) / 1000
    row = page.inner_text("[data-testid=cmp-mab]")
    assert f"+{mab:.2f} km" in row, row
    page.get_by_role("button", name="Scenario ·").click()
    chip = page.query_selector(".presets button:has-text('Maraimalai Adigal closed')").get_attribute("data-delta")
    assert chip == f"+{mab:.1f} km"
    page.get_by_role("button", name="Scenario ·").click()          # close the menu again
    page.click("[data-testid=cmp-mab]")
    wait_text(page, ".headline .num", lambda t: t.startswith(f"+{mab:.1f}"))
    allc = [c["id"] for x in meta["crossings"] for c in x["carriageways"]]
    top = httpx.post(f"{URL}/api/interventions", json={"scenario": {"rule": "B", "closed_carriageways": allc}}).json()["ranking"][0]
    rec = page.inner_text("[data-testid=cmp-recovery]")
    assert "best repair" in rec.lower() and top["label"] in rec
    assert f"{top['recovered_m'] / 1000:.2f} km" in rec and f"{top['share_of_disruption'] * 100:.1f}%" in rec


def test_analysis_drawer_narrows_the_map_and_it_keeps_rendering(page):
    # the map is full width; opening the analysis drawer narrows it temporarily, closing restores it
    w0 = page.evaluate("document.querySelector('.map').getBoundingClientRect().width")
    page.click("button:has-text('View analysis')")
    page.wait_for_selector("aside.right", state="visible")
    time.sleep(0.8)
    w1 = page.evaluate("document.querySelector('.map').getBoundingClientRect().width")
    assert w1 < w0 - 300 and page.evaluate("document.querySelector('.maplibregl-canvas').clientWidth") >= w1 - 2
    page.get_by_role("button", name="Close panel").click()
    time.sleep(0.8)
    w2 = page.evaluate("document.querySelector('.map').getBoundingClientRect().width")
    assert w2 >= w0 - 2 and page.evaluate("document.querySelector('.maplibregl-canvas').clientWidth") >= w2 - 2


def test_small_screen_uses_drawers_over_a_full_width_map():
    with sync_api.sync_playwright() as p:
        b = p.chromium.launch(channel="chrome", headless=True)
        pg = b.new_page(viewport={"width": 1024, "height": 680})
        pg.goto(URL, wait_until="load")
        pg.wait_for_selector(".scen-btn")
        assert pg.evaluate("document.querySelector('.map').getBoundingClientRect().width") >= 1000
        assert not pg.is_visible(".right")
        pg.click("button:has-text('View analysis')")
        pg.wait_for_selector(".right", state="visible")
        assert pg.evaluate("document.querySelector('.map').getBoundingClientRect().width") >= 1000   # a drawer over the map, not beside it
        pg.click(".left-pill")
        pg.wait_for_selector(".left", state="visible")
        assert not pg.is_visible(".right")                     # one panel at a time
        b.close()


def test_key_finding_and_recovery_share_visible_without_scrolling():
    with sync_api.sync_playwright() as p:
        b = p.chromium.launch(channel="chrome", headless=True)
        pg = b.new_page(viewport={"width": 1280, "height": 720})          # standard laptop screen
        pg.goto(URL, wait_until="load")
        go_view(pg, "Compare")
        pg.wait_for_selector("[data-testid=key-finding] .kf.rec", timeout=30000)
        box = pg.query_selector("[data-testid=key-finding] .kf.rec").bounding_box()
        assert box and box["y"] + box["height"] <= 720                       # above the fold
        assert "%" in pg.inner_text("[data-testid=key-finding] .kf.rec")
        b.close()


def test_zero_impact_closure_completes_its_pipeline(page):
    card = ask(page, "Thiru Vi Ka bridge is blocked.")
    states = {li.get_attribute("data-stage"): li.get_attribute("data-state")
              for li in card.query_selector_all("[data-testid=pipeline] li")}
    assert "pending" not in states.values(), states                         # nothing left looking unfinished
    assert states["impact"] == "done"


def test_cards_show_origin_and_go_historical_after_a_rule_change(page):
    card = ask(page, "What if Kotturpuram bridge were closed?")
    origin = card.query_selector("[data-testid=card-origin]").inner_text()
    assert "no closures" in origin and "Public hospitals" in origin
    assert card.query_selector("[data-testid=card-stale]") is None
    page.get_by_text("General hospitals").click()
    wait_text(page, ".kicker", lambda t: "general" in t.lower())
    assert card.query_selector("[data-testid=card-stale]") is not None      # historical result is labelled as such


def test_case_file_opens_from_agent_card_and_memory(page):
    card = ask(page, "What if Kotturpuram bridge were closed?")
    card.query_selector("[data-testid=open-case]").click()
    cf = page.wait_for_selector("[data-testid=case-file] .case-body", timeout=20000)
    t = cf.inner_text()
    assert "reported, unverified" in t.lower() and "+1.63 km" in t and "Recommendation" in t
    assert "investigate_disruption" in t and "FalkorDB operations" in t                 # what was actually executed
    page.click("text=Show the Cypher traversal")
    assert "MATCH (i:Investigation" in page.inner_text("[data-testid=case-cypher]")
    page.click("[data-testid=case-file] button:has-text('Close')")
    go_view(page, "History")
    page.wait_for_selector("[data-testid=memory-view] .mem-card", timeout=20000)
    page.click("[data-testid=memory-view] .mem-card >> nth=0 >> button:has-text('Case file')")
    assert page.wait_for_selector("[data-testid=case-file] .case-body", timeout=20000)
