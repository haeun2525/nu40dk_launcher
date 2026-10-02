#!/bin/zsh
# Double-click to run the launcher. A Terminal window opens and waits for button presses.
cd "$(dirname "$0")"
exec /usr/bin/python3 launcher.py
