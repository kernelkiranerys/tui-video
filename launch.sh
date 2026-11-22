#!/usr/bin/env bash
set -eo pipefail

exec nix-shell ./whisperx-shell.nix --run "
  source $HOME/.whisperx-venv/bin/activate
  cd .
  python tui.py $*
"
