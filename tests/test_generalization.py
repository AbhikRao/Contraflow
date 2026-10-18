"""Adversarial generalization set (scripts/generalization_eval.py) — deterministic planner in the suite; the live-model
run is opt-in (CONTRAFLOW_TEST_LLM=1). Pass conditions are properties checked against independent evaluations."""
import importlib.util
import os
from pathlib import Path

import pytest

spec = importlib.util.spec_from_file_location("generalization_eval",
                                              Path(__file__).resolve().parents[1] / "scripts" / "generalization_eval.py")
GE = importlib.util.module_from_spec(spec)
spec.loader.exec_module(GE)
LLM = os.environ.get("CONTRAFLOW_TEST_LLM") == "1"


@pytest.mark.parametrize("case", GE.CASES, ids=lambda c: c.__name__)
def test_generalization_deterministic(case):
    ok, detail, r = case("deterministic")
    assert ok, (detail, r["message"][:400])


@pytest.mark.skipif(not LLM, reason="set CONTRAFLOW_TEST_LLM=1 with Ollama running")
@pytest.mark.parametrize("case", [GE.c1_new_incident, GE.c3_negated, GE.c4_constraint_changes_preference,
                                  GE.c10_unauthorized_model_action], ids=lambda c: c.__name__)
def test_generalization_live(case):
    ok, detail, r = case("llm")
    assert r["mode"]["kind"] == "llm" and ok, (detail, r["message"][:400])
