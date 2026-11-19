#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")"

BINARIO="./dist/UNESR_Horarios/UNESR_Horarios"
if [ -x "$BINARIO" ]; then
  exec "$BINARIO" "$@"
fi

if [ ! -d ".venv" ]; then
  echo "Creando entorno virtual..."
  python3 -m venv .venv
fi
source .venv/bin/activate
python -m pip install -r requirements.txt --quiet
python main.py "$@"
