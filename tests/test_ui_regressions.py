"""Browser regressions (Playwright + installed Chrome) for audit findings F2 (UI) and F7.
Requires the built app served at CF_URL (default http://127.0.0.1:8000); skipped when not reachable."""
import os
import time

import httpx
import pytest

URL = os.environ.get("CF_URL", "http://127.0.0.1:8000")
sync_api = pytest.importorskip("playwright.sync_api")
from ui_nav import go_view, open_analysis, preset, show_agent, use_rules  # noqa: E402


def _ready():
    try:
        h = httpx.get(f"{URL}/api/health", timeout=2).json()
        return h.get("frontend_built", True) and httpx.get(URL, timeout=2).status_code == 200
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


def wait_text(pg, sel, pred, timeout=15):
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


def test_f7_stale_proposal_cannot_overwrite_newer_rule(page):
    preset(page, "All river crossings closed")
    show_agent(page)
    use_rules(page)
    page.fill(".agent-input textarea", "Maraimalai Adigal bridge has reopened.")
    page.keyboard.press("Enter")
    page.wait_for_selector(".prop button")
    page.get_by_text("General hospitals").click()
    wait_text(page, ".kicker", lambda t: "general" in t.lower())
    wait_text(page, ".prop", lambda t: "out of date" in t)
    assert page.query_selector(".prop button:has-text('Apply')") is None
    page.click(".prop button:has-text('Ask again')")       # re-asks on the CURRENT scenario
    end = time.time() + 15
    while time.time() < end and len(page.query_selector_all(".a-card")) < 2:
        time.sleep(0.2)
    cards = page.query_selector_all(".a-card")
    assert len(cards) == 2 and cards[1].query_selector(".prop button:has-text('Apply')")
    cards[1].query_selector(".prop button:has-text('Apply')").click()
    wait_text(page, ".kicker", lambda t: "general" in t.lower())    # rule never silently reverted


def test_f7_reset_clears_intervention_loading(page):
    preset(page, "All river crossings closed")
    preset(page, "Baseline")       # immediately reset
    time.sleep(3)
    open_analysis(page, "Recovery ranking")      # baseline opens the Compare tab since the UI polish
    txt = page.inner_text(".right")
    assert "Evaluating interventions" not in txt and "No disruption" in txt


def test_f7_rapid_changes_show_ranking_for_final_scenario_only(page):
    for label in ["Maraimalai Adigal closed", "Thiru Vi Ka closed", "All river crossings closed",
                  "Maraimalai Adigal closed"]:
        preset(page, label)
        time.sleep(0.05)
    open_analysis(page, "Recovery ranking")
    head = wait_text(page, ".rank-head", lambda t: "candidates evaluated" in t)
    api = httpx.post(f"{URL}/api/interventions", json={"scenario": {"rule": "B", "closed_carriageways": [
        "maraimalai_adigal_bridge:northbound", "maraimalai_adigal_bridge:southbound"]}}).json()
    assert head.startswith(f"{len(api['ranking'])} candidates")
    wait_text(page, ".headline .num", lambda t: t.strip() == "+11.9 km")


def test_f7_agent_conversation_survives_tab_switch(page):
    show_agent(page)
    use_rules(page)
    page.fill(".agent-input textarea", "What should we reopen first?")
    page.keyboard.press("Enter")
    page.wait_for_selector(".a-card")
    page.get_by_text("Edit scenario", exact=True).click()
    show_agent(page)
    assert page.query_selector(".a-card") is not None and page.query_selector(".u-msg") is not None


def test_f2_ui_shows_disconnecting_candidate_as_harmful(page):
    page.get_by_text("Edit scenario", exact=True).click()           # manual scenario editing sits behind "Edit scenario"
    page.click(".xcard:has-text('Maraimalai Adigal') .cw:has-text('Northbound') .switch")
    open_analysis(page, "Recovery ranking")
    wait_text(page, ".rank-head", lambda t: "candidates evaluated" in t)
    page.click("details.harmful summary")
    harm = page.inner_text("details.harmful")
    assert "Taluk Office Road" in harm and "CUTS OFF: Little Mount" in harm
    assert "Taluk Office Road (33 m)" not in (page.inner_text("details.zero:not(.harmful)")
                                               if page.query_selector("details.zero:not(.harmful)") else "")


def test_r5_ui_applies_candidate_exactly_as_predicted(page):
    preset(page, "Maraimalai Adigal closed")
    open_analysis(page, "Recovery ranking")
    wait_text(page, ".rank-head", lambda t: "candidates evaluated" in t)
    best = page.inner_text(".cand.best")
    assert "Reopen all of Maraimalai Adigal Bridge" in best and "recovers 11.92 km" in best
    page.click(".cand.best .cand-head button")                # applied by POST /api/apply_candidate
    wait_text(page, ".headline .num", lambda t: t.strip() in ("79.6 km", "0.0 km"))   # predicted: full recovery


def test_r1_ui_negated_instruction_proposes_nothing(page):
    show_agent(page)
    use_rules(page)
    page.fill(".agent-input textarea", "Do not close Saidapet bridge.")
    page.keyboard.press("Enter")
    page.wait_for_selector(".a-card")
    assert page.query_selector(".a-card .prop") is None
    assert "not" in page.inner_text(".a-card .a-text").lower()


# ---------------------------------------------------------------- B3: late candidate-application responses
class Held:
    """Intercepts /api/apply_candidate and withholds each response until the test releases it."""

    def __init__(self, page):
        self.routes = []
        page.route("**/api/apply_candidate", lambda route: self.routes.append(route))
        self.page = page

    def wait(self, n, timeout=10):
        end = time.time() + timeout
        while len(self.routes) < n and time.time() < end:
            self.page.wait_for_timeout(100)
        assert len(self.routes) >= n, "apply request not issued"

    def release(self, i=0, fail=False):
        r = self.routes[i]
        r.fulfill(status=409, json={"detail": {"code": "stale_candidate", "message": "stale"}}) if fail else r.continue_()
        self.page.wait_for_timeout(1500)


def headline(pg):
    return pg.inner_text(".headline .num").strip()


def test_b3_late_apply_response_cannot_override_rule_change(page):
    held = Held(page)
    preset(page, "Maraimalai Adigal closed")
    open_analysis(page, "Recovery ranking")
    wait_text(page, ".rank-head", lambda t: "candidates evaluated" in t)
    page.click(".cand.best .cand-head button")
    held.wait(1)
    page.get_by_text("General hospitals").click()                         # newer user choice while response pending
    wait_text(page, ".kicker", lambda t: "general" in t.lower())
    held.release(0)                                                        # late response arrives
    page.wait_for_timeout(1500)
    assert "general" in page.inner_text(".kicker").lower()                  # never reverted to Public hospitals
    assert headline(page) == "+6.7 km"                                      # still the closure, recomputed under rule A
    assert page.query_selector("[data-testid=applying]") is None


def test_b3_reset_during_application_is_not_undone(page):
    held = Held(page)
    preset(page, "All river crossings closed")
    open_analysis(page, "Recovery ranking")
    wait_text(page, ".rank-head", lambda t: "candidates evaluated" in t)
    page.click(".cand.best .cand-head button")
    held.wait(1)
    preset(page, "Baseline")
    wait_text(page, ".headline .num", lambda t: t.strip() == "79.6 km")
    held.release(0)
    open_analysis(page, "Recovery ranking")      # baseline opens the Compare tab since the UI polish
    assert headline(page) == "79.6 km" and "No disruption" in page.inner_text(".right")
    assert page.query_selector("[data-testid=applying]") is None


def test_b3_rapid_consecutive_applications_apply_only_the_first(page):
    held = Held(page)
    preset(page, "All river crossings closed")
    open_analysis(page, "Recovery ranking")
    wait_text(page, ".rank-head", lambda t: "candidates evaluated" in t)
    buttons = page.query_selector_all(".cand .cand-head button")
    buttons[0].click()
    buttons[2].click()                                                     # second click before the first returns
    held.wait(2)
    held.release(0)                                                        # first: Reopen all of Maraimalai Adigal
    wait_text(page, ".headline .num", lambda t: t.strip() == "+1.6 km")
    held.release(1)                                                        # second: captured at the old revision
    assert headline(page) == "+1.6 km"                                     # ignored; no mixing of results
    assert page.query_selector("[data-testid=applying]") is None


def test_b3_failed_application_clears_loading(page):
    held = Held(page)
    preset(page, "Maraimalai Adigal closed")
    open_analysis(page, "Recovery ranking")
    wait_text(page, ".rank-head", lambda t: "candidates evaluated" in t)
    page.click(".cand.best .cand-head button")
    held.wait(1)
    held.release(0, fail=True)
    assert page.query_selector("[data-testid=applying]") is None
    assert headline(page) == "+11.9 km"                                     # scenario unchanged


# ---------------------------------------------------------------- B4: provenance labels
def test_b4_deterministic_text_is_not_labelled_tool_composed(page):
    show_agent(page)
    use_rules(page)
    page.fill(".agent-input textarea", "Maraimalai Adigal must not be reserved for emergency vehicles.")
    page.keyboard.press("Enter")
    page.wait_for_selector(".a-card .ground")
    g = page.query_selector(".a-card .ground")
    assert g.get_attribute("data-source") == "deterministic"
    assert "System-generated" in g.inner_text() and "composed from graph tool results" not in g.inner_text()
    assert page.query_selector(".a-card .prop") is None
