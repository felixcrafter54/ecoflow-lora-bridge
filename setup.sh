#!/usr/bin/env bash
set -euo pipefail

# Getesteter Stand von ha-ef-ble. Beim Hochziehen siehe README.
EF_BLE_COMMIT="affbb60"
EF_BLE_REPO="https://github.com/rabits/ha-ef-ble"

DIR="$(cd "$(dirname "$0")" && pwd)"
ROLLE="${1:-}"

if [[ "$ROLLE" != "garage" && "$ROLLE" != "haus" ]]; then
    echo "Aufruf: $0 garage|haus"
    exit 1
fi

echo ">> Systempakete"
sudo apt update
sudo apt install -y python3-venv python3-dev git
[[ "$ROLLE" == "garage" ]] && sudo apt install -y bluez sqlite3

echo ">> venv"
python3 -m venv "$DIR/.venv"
"$DIR/.venv/bin/pip" install -U pip
if [[ "$ROLLE" == "garage" ]]; then
    "$DIR/.venv/bin/pip" install -r "$DIR/requirements.txt"
else
    "$DIR/.venv/bin/pip" install -r "$DIR/requirements-gateway.txt"
fi

if [[ "$ROLLE" == "garage" ]]; then
    echo ">> ha-ef-ble @ ${EF_BLE_COMMIT}"
    [[ -d "$DIR/vendor/ha-ef-ble" ]] || \
        git clone "$EF_BLE_REPO" "$DIR/vendor/ha-ef-ble"
    git -C "$DIR/vendor/ha-ef-ble" fetch --all --quiet
    git -C "$DIR/vendor/ha-ef-ble" checkout --quiet "$EF_BLE_COMMIT"

    echo ">> Bluetooth prüfen"
    if grep -q "console=serial0" /boot/firmware/cmdline.txt 2>/dev/null; then
        echo "   ACHTUNG: console=serial0 steht in cmdline.txt."
        echo "   Bluetooth wird damit sporadisch ausfallen. Siehe README."
    fi
fi

sudo usermod -aG dialout "$USER"

if [[ ! -f "$DIR/.env" ]]; then
    cp "$DIR/.env.example" "$DIR/.env"
    chmod 600 "$DIR/.env"
    echo ">> .env angelegt - bitte ausfüllen"
fi

echo ">> fertig. Neu anmelden, damit die dialout-Gruppe greift."
