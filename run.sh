#!/usr/bin/env sh
# Linux / macOS 启动脚本
cd "$(dirname "$0")"
if [ -x ./venv/bin/python ]; then PY=./venv/bin/python; else PY=python3; fi
exec "$PY" -m pdfedit "$@"
