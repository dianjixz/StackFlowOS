#!/usr/bin/env bash
# Build the Astryx console (webui/) into stackflowos/webassets.
#
# The Python hub only ever serves the built files: Node is needed to *change*
# the console, never to run it.  Run from anywhere.
set -euo pipefail
cd "$(dirname "$0")/.."

if ! command -v npm >/dev/null 2>&1; then
  echo "error: npm is required to build the console (webui/)" >&2
  exit 1
fi
if [ ! -d webui/node_modules ]; then
  echo "==> installing webui dependencies"
  (cd webui && npm ci)
fi
echo "==> typecheck"
(cd webui && npx tsc --noEmit -p tsconfig.json)
echo "==> build -> stackflowos/webassets"
(cd webui && npx vite build --outDir ../stackflowos/webassets --emptyOutDir)
echo "==> done"
