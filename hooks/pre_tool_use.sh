#!/usr/bin/env bash
# route-guard: pre_tool_use shim

set -euo pipefail
DIR="$( cd "$( dirname "${BASH_SOURCE[0]}" )" && pwd )"
PLUGIN_ROOT="$( dirname "$DIR" )"
PYTHONPATH="$PLUGIN_ROOT" exec python3 -m route_guard.cli pre_tool_use
