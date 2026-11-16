#!/bin/sh
# Starts the gemini-delegate server with the chosen runtime.
# CAPOCANTIERE_RUNTIME=python | node | auto (default: python3 if available, otherwise node).
dir=$(dirname "$0")
case "${CAPOCANTIERE_RUNTIME:-auto}" in
  python) exec python3 "$dir/server.py" "$@" ;;
  node) exec node "$dir/server.mjs" "$@" ;;
  *)
    if command -v python3 >/dev/null 2>&1; then exec python3 "$dir/server.py" "$@"; fi
    if command -v node >/dev/null 2>&1; then exec node "$dir/server.mjs" "$@"; fi
    echo "capocantiere: neither python3 nor node found on PATH" >&2
    exit 127
    ;;
esac
