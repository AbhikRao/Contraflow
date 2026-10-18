#!/usr/bin/env bash
# Starts the pinned FalkorDB used by Contraflow for local development (docker compose not required).
# Non-destructive: an existing contraflow-falkordb container is started as is (never removed). A new container stores
# its data in the named volume contraflow-falkordb-data, so it survives `docker rm` and recreation.
set -euo pipefail
IMAGE="falkordb/falkordb:6.0.2@sha256:107455e862a824e45e8e9f5a7fbfdf5e00a08b14e13eae2577c72fd9e3f59ba6"
if docker container inspect contraflow-falkordb >/dev/null 2>&1; then
  docker start contraflow-falkordb >/dev/null
  echo "started existing contraflow-falkordb"
else
  docker run -d --name contraflow-falkordb -p 127.0.0.1:6379:6379 -p 127.0.0.1:3000:3000 \
    -v contraflow-falkordb-data:/var/lib/falkordb/data \
    -e REDIS_ARGS="--appendonly yes --appendfsync everysec" "$IMAGE" >/dev/null
  echo "created contraflow-falkordb (data volume: contraflow-falkordb-data)"
fi
