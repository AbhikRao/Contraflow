# Demo video script — 110–120 seconds

> **Navigation in the final UI:** the top navigation is **Investigate · Compare · History**. Presets are in the
> **Scenario** menu on the map. Recovery ranking, Sensitivity and Evidence are sections of the **View analysis** drawer.
> The beats below still apply; reach each view through these controls.

**Honesty rules for the recording**
- Record the agent turn **live in LLM mode** (header shows `qwen2.5:7b ready`; the card says "Live LLM"). Never present
  the rule-based planner as the model. If you trim the model's wait in editing, show a visible "time cut" marker.
- Say "modeled distance", never "travel time", "safe" or "capacity". The Tabletop-sandbox badge stays on screen.
- Do not claim a stage before it appears on screen.

**Setup** (before pressing record)
1. `colima start` (or Docker Desktop), FalkorDB running (`./infra/start_falkordb.sh` or `docker compose up`), Ollama
   running with `qwen2.5:7b` pulled; run one throw-away agent question so the model is loaded.
2. Restart the app by exact process name if it was running from older code.
3. Browser at about 1440 × 900, **Baseline**, rule **Public hospitals**, Planning-agent tab, mode **LLM**.
4. `.venv/bin/python scripts/record_demo.py` — confirm Thiru Vi Ka 0.0 km, Maraimalai Adigal +11.92 km, all crossings
   +13.55 km, 87.9 % recovery. If anything differs, say the new numbers.

| Time | Screen | Narration (approx.) |
|---|---|---|
| 0:00–0:15 | Compare tab: key-finding strip and bars. Click **Close: Thiru Vi Ka**, then **Close: Maraimalai Adigal**. | "Real OpenStreetMap roads of Chennai's Adyar corridor, in FalkorDB. Close the big Thiru Vi Ka bridge: zero added modeled distance. Close Maraimalai Adigal at Saidapet: plus 11.92 km, Guindy alone 3.6 more. Modeled distance to the nearest public hospital — not emergency outcomes." |
| 0:15–0:30 | Click the **87.9 %** tile (opens Recovery for all crossings closed); hover the top candidate. | "With every crossing closed it's 13.55 km. Each repair is evaluated on its own graph copy; reopening Maraimalai Adigal alone recovers 87.9 % — and these are the neighbourhoods that benefit." |
| 0:30–1:05 | **Baseline.** Agent: *"A volunteer says Kotturpuram bridge may be blocked by fallen trees. Who would need a detour, keeping Maraimalai Adigal southbound reserved for ambulances?"* | While the progress panel streams: "This is the local Qwen model choosing graph tools. Only completed operations appear — here's the investigation on a graph copy… ranking under the hypothesis…" |
| 1:05–1:25 | Finished card: pipeline, **Field report · unverified**, **Assumption · analysis only** (southbound only), affected neighbourhoods (hover → map), alternatives. | "The report stays unverified. The bridge is evaluated in both directions because no direction was given; the reservation is southbound only, as stated. Three neighbourhoods need a detour; the best repair is Kotturpuram northbound. Nothing has changed — the human decision is still open." |
| 1:25–1:45 | Click **Propose closing Kotturpuram Bridge** → **Apply**. Then open **Memory**. | "Turning it into a change needs my approval. Memory is a FalkorDB graph of reports, places, findings and decisions — and the earlier finding is re-checked against the new scenario: superseded, because the closure is now applied." |
| 1:45–2:00 | **Evidence** tab: query log (`GRAPH.COPY`, `algo.SPpaths`, memory writes). | "Every number traces to these FalkorDB operations — graph copies, shortest paths, memory relationships. Contraflow: a graph-native planning agent you can verify." |

Timing note: the agent turn took 15–27 s in this phase's live runs. The 0:30–1:05 slot assumes that wait; the
following rows are short. Total 110–120 s with brisk narration; drop the Evidence row to 5 s if you run long.

Optional extra clip (not in the main video): the **Sensitivity** tab — read the scope card and the "Read with care"
caveat in full; a rushed version would overstate it.
