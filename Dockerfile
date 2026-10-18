# Contraflow app image: React build + FastAPI. FalkorDB runs as a separate pinned container.
FROM node:22.11.0-alpine AS web
WORKDIR /web
COPY web/package.json web/package-lock.json ./
RUN npm ci
COPY web/ ./
RUN npm run build

FROM python:3.12.7-slim
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY contraflow/ contraflow/
COPY data/processed/ data/processed/
COPY --from=web /web/dist web/dist
ENV FALKORDB_HOST=falkordb FALKORDB_PORT=6379 \
    CONTRAFLOW_LLM_BASE_URL=http://host.docker.internal:11434/v1
EXPOSE 8000
CMD ["uvicorn", "contraflow.api:app", "--host", "0.0.0.0", "--port", "8000"]
