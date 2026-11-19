#!/usr/bin/env bash
set -euo pipefail
cd -- "$(dirname -- "$0")"
mode="${1:-console}"
case "$mode" in
    console|install-task|check|preview) ;;
    *) echo 'Использование: ./start.sh {console|install-task|check|preview}' >&2; exit 2 ;;
esac

if [[ ! -x .venv/Scripts/python.exe ]]; then
    if command -v py >/dev/null; then
        py -3.13 -m venv .venv
    else
        python -m venv .venv
    fi
fi
python_bin=./.venv/Scripts/python.exe
if [[ ! -f .venv/requirements.applied ]] || ! cmp -s requirements.txt .venv/requirements.applied; then
    "$python_bin" -m pip install --disable-pip-version-check -r requirements.txt
    cp requirements.txt .venv/requirements.applied
fi

case "$mode" in
    console) exec "$python_bin" -X utf8 -B main.py ;;
    check) exec "$python_bin" -X utf8 -B main.py --check-models ;;
    preview) exec "$python_bin" -X utf8 -B main.py --preview "${2:-}" ;;
    install-task) exec powershell.exe -NoProfile -File install_task.ps1 ;;
esac
