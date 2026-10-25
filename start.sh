#!/usr/bin/env bash
set -euo pipefail

sudo apt-get update
sudo apt-get install -y ffmpeg fonts-noto-core
pip install -r requirements.txt

if [ -z "$MEDIA_DIR" ]; then MEDIA_DIR=./media; fi
if [ -z "$PUBLIC_BASE" ]; then PUBLIC_BASE=http://localhost:8000; fi
if [ -z "$FONT_NAME" ]; then FONT_NAME="Noto Naskh Arabic"; fi
export MEDIA_DIR PUBLIC_BASE FONT_NAME

mkdir -p "$MEDIA_DIR"
exec uvicorn server:app --host 0.0.0.0 --port 8000
