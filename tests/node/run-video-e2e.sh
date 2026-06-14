#!/usr/bin/env bash
# Run the video-assembly end-to-end harness against an ephemeral Redis.
# The stack's own redis uses `expose` (not host-published), so we spin a throwaway.
#
#   tests/node/run-video-e2e.sh [sceneCount ...]   (default: 5 and 12)
set -euo pipefail
cd "$(dirname "$0")/../.."

PORT="${E2E_REDIS_PORT:-6399}"
NAME="rcs-video-e2e-redis"
docker rm -f "$NAME" >/dev/null 2>&1 || true
docker run -d --rm --name "$NAME" -p "${PORT}:6379" redis:7-alpine >/dev/null
trap 'docker rm -f "$NAME" >/dev/null 2>&1 || true' EXIT
sleep 1

COUNTS=("$@"); [ ${#COUNTS[@]} -eq 0 ] && COUNTS=(5 12)
export REDIS_URL="redis://127.0.0.1:${PORT}"
export VIDEO_WORKER=1                # we ARE the worker process here (lifts ffmpeg guard)
export VIDEO_WIDTH=640 VIDEO_HEIGHT=360 VIDEO_PRESET=ultrafast   # keep the encode quick

rc=0
for n in "${COUNTS[@]}"; do
  echo "=== e2e: $n scenes ==="
  node tests/node/video_e2e.mjs "$n" || rc=1
done
exit $rc
