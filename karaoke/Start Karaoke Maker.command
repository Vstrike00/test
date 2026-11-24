#!/bin/bash
# Double-click this file in Finder to start Karaoke Maker.
cd "$(dirname "$0")" || exit 1

echo "🎤 Karaoke Maker"
echo

# Homebrew lives in different places on Apple Silicon vs Intel Macs
for p in /opt/homebrew/bin /usr/local/bin; do
  [ -d "$p" ] && export PATH="$p:$PATH"
done

if ! command -v ffmpeg >/dev/null 2>&1; then
  if command -v brew >/dev/null 2>&1; then
    echo "Installing ffmpeg (one time only)..."
    brew install ffmpeg || { echo "ffmpeg install failed."; read -r -p "Press Enter to close."; exit 1; }
  else
    echo "ffmpeg is required. Install Homebrew from https://brew.sh, then run:"
    echo "    brew install ffmpeg"
    read -r -p "Press Enter to close."
    exit 1
  fi
fi

PY=$(command -v python3)
if [ -z "$PY" ]; then
  echo "Python 3 is required. Install it with:  brew install python"
  read -r -p "Press Enter to close."
  exit 1
fi

if [ ! -x .venv/bin/python ]; then
  echo "Setting up (one time only, takes a minute)..."
  "$PY" -m venv .venv || { read -r -p "Could not create venv. Press Enter."; exit 1; }
fi
if [ ! -f .venv/.installed ] || [ requirements.txt -nt .venv/.installed ]; then
  .venv/bin/pip install --upgrade pip -q
  .venv/bin/pip install -r requirements.txt -q && touch .venv/.installed
fi

exec .venv/bin/python app.py
