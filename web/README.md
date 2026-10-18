# Contraflow web UI

React 19 + TypeScript + MapLibre GL. The map renders road geometry served by the Contraflow API
(`/api/layers`); no tile or glyph server is needed.

```bash
npm ci
npm run dev      # http://127.0.0.1:5173, proxies /api to http://127.0.0.1:8000
npm run build    # writes dist/, served by FastAPI at http://127.0.0.1:8000
npm run lint
```

See the repository README for the full setup.
