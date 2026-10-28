#!/bin/zsh
# Starts the Titan daemon in this Terminal window. This window is the emergency STOP: SPACE / ENTER / s = STOP.
# Double-click it in Finder, or: open -a Terminal start-daemon.command
# Extra args pass through, e.g. ./start-daemon.command --pin 123456   or   --address <uuid>   or   --fake
cd "$(dirname "$0")" || exit 1
exec .venv/bin/python -m titan daemon "$@"
