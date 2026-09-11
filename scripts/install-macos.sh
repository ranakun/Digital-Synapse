#!/bin/sh
# Agent-executable local install. Does not edit shell profiles, Codex settings or user knowledge.
set -eu
[ "$(uname -s)" = Darwin ] || { echo 'This installer targets macOS.' >&2; exit 1; }
SYNAPSE_INSTALL_HOME=${1:-"$HOME/Library/Application Support/Digital Synapse"}
SYNAPSE_REPO=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
case "$SYNAPSE_INSTALL_HOME" in /*) ;; *) echo 'Choose an absolute installation path.' >&2; exit 1;; esac
if [ -e "$SYNAPSE_INSTALL_HOME" ] && [ ! -f "$SYNAPSE_INSTALL_HOME/.install-owner" ]; then
  echo 'The installation folder already exists. Choose a new folder; existing data will not be changed.' >&2
  exit 1
fi
mkdir -p "$SYNAPSE_INSTALL_HOME"
printf '%s\n' 'digital-synapse-public-v2' > "$SYNAPSE_INSTALL_HOME/.install-owner"
export UV_PYTHON_INSTALL_DIR="$SYNAPSE_INSTALL_HOME/tools/python"
export UV_CACHE_DIR="$SYNAPSE_INSTALL_HOME/tools/cache"
SYNAPSE_UV=$(command -v uv || true)
if [ -z "$SYNAPSE_UV" ]; then
  SYNAPSE_BOOTSTRAP=$(mktemp)
  trap 'rm -f "$SYNAPSE_BOOTSTRAP"' EXIT HUP INT TERM
  curl --proto '=https' --tlsv1.2 -LsSf https://astral.sh/uv/install.sh -o "$SYNAPSE_BOOTSTRAP"
  UV_UNMANAGED_INSTALL="$SYNAPSE_INSTALL_HOME/tools" sh "$SYNAPSE_BOOTSTRAP"
  SYNAPSE_UV="$SYNAPSE_INSTALL_HOME/tools/uv"
fi
"$SYNAPSE_UV" venv --python 3.12 --allow-existing "$SYNAPSE_INSTALL_HOME/runtime"
"$SYNAPSE_UV" pip install --python "$SYNAPSE_INSTALL_HOME/runtime/bin/python" --constraint "$SYNAPSE_REPO/requirements-mac.txt" "$SYNAPSE_REPO[ingest,embeddings,mcp]"
echo 'Runtime installed. The setup agent can now initialize the workspace with the agreed purpose and timezone.'
printf '%s\n' "$SYNAPSE_INSTALL_HOME/runtime/bin/synapse"
