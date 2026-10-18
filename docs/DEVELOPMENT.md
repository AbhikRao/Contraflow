# Development and reproducibility

Use a separate local checkout and development database. The test suite and evidence scripts create graphs, and some reload the base model. Do not point them at a running public demo or a recording session.

## Local setup

Requires Python 3.12, Node.js 22, a Docker engine and [uv](https://docs.astral.sh/uv/).

```bash
./infra/start_falkordb.sh
uv venv --python 3.12 .venv
uv pip install --python .venv/bin/python -r requirements-dev.txt
(cd web && npm ci)
./scripts/dev.sh
```

Frontend: http://127.0.0.1:5173; backend: http://127.0.0.1:8000. The database startup script preserves its named container and volume. Local investigation journals are stored in git-ignored `data/memory/`.

For the production frontend bundle:

```bash
(cd web && npm run build)
```

Configuration: `FALKORDB_HOST`, `FALKORDB_PORT`, optional `FALKORDB_USERNAME` / `FALKORDB_PASSWORD`, `CONTRAFLOW_LLM_BASE_URL` (default `http://127.0.0.1:11434/v1`), `CONTRAFLOW_LLM_MODEL` (default `qwen2.5:7b`) and optional `CONTRAFLOW_LLM_API_KEY`. Keep credentials out of Git.

The Docker image defaults to `http://host.docker.internal:11434/v1` for host Ollama. Host reachability depends on the Docker engine. For Colima, `http://host.lima.internal:11434/v1` may be required: set `CONTRAFLOW_LLM_BASE_URL` in the app service’s Compose `environment` mapping. Ollama must listen on an interface reachable from that VM; keep it private and firewall access appropriately. The web UI reports whether Qwen is actually available.

## Tests and recorded evaluations

Against your separate development database:

```bash
.venv/bin/pytest -q
```

Browser tests use Playwright and installed Chrome; see the individual test modules for prerequisites. Live-LLM cases are opt-in with `CONTRAFLOW_TEST_LLM=1` and require the local model.

A database-free model/data consistency check is also available:

```bash
.venv/bin/pytest --noconftest -p no:cacheprovider -q tests/test_model.py
```

To reproduce demo results and benchmarks on that development database:

```bash
.venv/bin/python scripts/record_demo.py
.venv/bin/python scripts/benchmark.py --llm
```

[Evaluation index](../results/README.md) explains the retained evidence. `scripts/build_model.py` rebuilds the processed dataset from the bundled raw OSM extract. Historical feasibility experiments are preserved in `feasibility/` with their reports in `docs/archive/`; they are distinct from the production routing engine.

## MCP integration

`contraflow.mcp_server` implements stdio JSON-RPC 2.0 with seven tools: `investigate_report`, `evaluate_closures`, `rank_repairs`, `recall_investigations`, `get_case_file`, `access_sensitivity` and `test_recommendation`. They cannot apply road changes, but investigations can write evidence to the configured memory graph. Use a separate development database for external-agent experiments.

For Claude Code, from the repository root:

```bash
claude mcp add contraflow -- "$PWD/.venv/bin/python" -m contraflow.mcp_server
```

Other MCP clients can run the same module with the checkout on `PYTHONPATH`. FalkorDB must be running. Example: “Investigate whether Kotturpuram Bridge is blocked, keeping Maraimalai Adigal southbound reserved for ambulances, then fetch the case file.”

## Persistence and public hosting

Compose stores FalkorDB data and the memory journal in named volumes. Stop, restart and container recreation retain them; `docker compose down -v` deletes them. `scripts/persistence_check.sh` exercises persistence in a separate Compose project on port 8099.

The rate-limited public deployment has its own configuration and isolation controls. See [DEPLOYMENT.md](DEPLOYMENT.md); ordinary development startup is not a public-hosting configuration.
