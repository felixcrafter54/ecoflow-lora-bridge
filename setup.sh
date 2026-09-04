#!/usr/bin/env bash
set -euo pipefail

EF_BLE_COMMIT="abc1234"          # getesteter Stand, nicht main
DIR="$(cd "$(dirname "$0")" && pwd)"

echo ">> System"
sudo apt update
sudo apt install -y python3-venv python3-dev git bluez sqlite3

echo ">> ha-ef-ble @ ${EF_BLE_COMMIT}"
if [ ! -d "$DIR/vendor/ha-ef-ble" ]; then
    git clone https://github.com/rabits/ha-ef-ble "$DIR/vendor/ha-ef-ble"
fi
git -C "$DIR/vendor/ha-ef-ble" fetch --all
git -C "$DIR/vendor/ha-ef-ble" checkout "$EF_BLE_COMMIT"

echo ">> venv"
python3 -m venv "$DIR/.venv"
"$DIR/.venv/bin/pip" install -U pip
"$DIR/.venv/bin/pip" install -r "$DIR/requirements.txt"

echo ">> Gruppen"
sudo usermod -aG dialout "$USER"

[ -f "$DIR/.env" ] || { cp "$DIR/.env.example" "$DIR/.env"; \
    chmod 600 "$DIR/.env"; echo ">> .env angelegt - bitte ausfuellen"; }

echo ">> fertig"
