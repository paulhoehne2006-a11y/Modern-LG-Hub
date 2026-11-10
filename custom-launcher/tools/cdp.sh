#!/bin/bash
# Wrapper for tools/cdp.js (Node 21 needs a flag for the built-in WebSocket client).
exec node --experimental-websocket --no-warnings "$(dirname "$0")/cdp.js" "$@"
