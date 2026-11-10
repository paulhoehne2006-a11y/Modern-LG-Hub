#!/bin/bash
# Wrapper for tools/localshot.js (Node 21 needs a flag for the built-in WebSocket client).
exec node --experimental-websocket --no-warnings "$(dirname "$0")/localshot.js" "$@"
