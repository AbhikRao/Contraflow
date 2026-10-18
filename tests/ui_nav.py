"""Navigation helpers for the browser tests (second-pass UI: Investigate / Compare / History).

Assertions are unchanged by the redesign; only the way a test reaches a control differs:
presets live in the map's Scenario menu, the repair ranking / sensitivity / evidence live in the analysis drawer
('View analysis'), the comparison is the Compare view, graph memory is the History view, and the agent's mode
control sits behind the planner status indicator."""


def show_agent(pg):
    """Make the investigation input visible: expand the card and leave the manual-scenario view."""
    if pg.query_selector(".left-pill"):
        pg.click(".left-pill")
    back = pg.query_selector("text=← Back to investigation")
    if back and back.is_visible():
        back.click()
    pg.wait_for_selector(".agent-input textarea", state="visible")


def use_rules(pg):
    """Switch the planner to the deterministic rule-based mode (no LLM) for reproducible runs."""
    show_agent(pg)
    pg.click("button.llm-status")
    pg.get_by_role("button", name="Rules").click()


def preset(pg, label):
    """Apply a scenario preset from the map's Scenario menu."""
    pg.get_by_role("button", name="Scenario ·").click()
    pg.get_by_text(label, exact=True).click()


def open_analysis(pg, section=None):
    """Open the analysis drawer (idempotent) and optionally a section: 'Recovery ranking', 'Road sensitivity', 'Evidence and provenance'."""
    if not pg.query_selector("aside.right .acc, aside.right .impact") or not pg.is_visible("aside.right"):
        btn = pg.query_selector("button:has-text('View analysis')")
        if btn and btn.is_visible():
            btn.click()
        else:
            pg.get_by_role("button", name="Investigate", exact=True).first.click()
            pg.click("button:has-text('View analysis')")
    pg.wait_for_selector("aside.right", state="visible")
    if section:
        h = pg.locator(f".acc-h:has-text('{section}')")
        if h.get_attribute("aria-expanded") != "true":
            h.click()


def go_view(pg, name):
    pg.get_by_role("button", name=name, exact=True).first.click()
