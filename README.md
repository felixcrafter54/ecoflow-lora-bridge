# EcoFlow LoRa Bridge

Cloud-free EcoFlow monitoring and control for off-grid sites without network
coverage — BLE to LoRa to Home Assistant.

Reads an EcoFlow power station over Bluetooth LE, computes energy counters
locally, and relays the data over a Meshtastic LoRa link to a gateway that
publishes it to Home Assistant via MQTT auto-discovery. Outputs can be
switched back over the same link.

Built for a detached garage on an off-grid island system, several hundred
metres from the house. No EcoFlow cloud, no internet, no WiFi at the remote end.

## Architecture

    Garage                                  House
    ─────────────────────────────           ──────────────────────────────
    EcoFlow Delta 3 Max Plus
      │ BLE (encrypted, local)
    Raspberry Pi 3B
      │ bridge.py ── SQLite (energy counters)
      │ USB
    Meshtastic node ──── LoRa 868 MHz ───► Meshtastic node (USB → LXC)
                                             │ gateway.py
                                             │ MQTT
                                           Mosquitto → Home Assistant

Three decoupled intervals:

| what        | interval                       |
|-------------|--------------------------------|
| integration | every BLE packet (~1 s)        |
| SQLite row  | on change, at least every 30 s |
| LoRa packet | 60 s min, 300 s max            |

Energy is integrated at full resolution regardless of how often packets
are sent over the air.

## Hardware

- Raspberry Pi 3B (or newer) with Raspberry Pi OS Lite
- EcoFlow Delta 3 Max Plus (SN prefix `D3M1`)
- 2× Meshtastic node, EU_868 — e.g. Seeed XIAO ESP32S3 + Wio-SX1262
- USB SSD for the database (optional but recommended)

## Setup

    git clone git@github.com:felixcrafter54/ecoflow-lora-bridge.git
    cd ecoflow-lora-bridge
    ./setup.sh
    cp .env.example .env && chmod 600 .env
    nano .env

Find the station's MAC and serial:

    python src/tools/scan.py

Test the connection and controls interactively:

    python src/tools/ctl.py

## Configuration

See `.env.example`. The Meshtastic channel PSK is **not** stored here — it
lives on the node itself, set once via `meshtastic --ch-set psk random`.

## Commands (house → garage)

Sent as a Meshtastic direct message to the garage node:

| command    | effect                    |
|------------|---------------------------|
| `ac 0/1`   | AC output                 |
| `ac2 0/1`  | second AC output          |
| `dc 0/1`   | 12 V output               |
| `chg <W>`  | AC charging power         |
| `lmin <%>` | discharge limit           |
| `lmax <%>` | charge limit              |
| `ping`     | request an immediate packet |
| `reboot`   | restart the Pi            |

Commands are whitelisted and only accepted from the node configured as
`PEER_NODE_ID`.

## Notes and gotchas

Things that cost time during development:

- **`console=serial0,115200` must be removed from `/boot/firmware/cmdline.txt`.**
  The serial console and the Bluetooth chip share the same UART on the Pi 3B.
  With it present, BT initialisation fails intermittently — `hci0` exists but
  is dead, and bleak reports `NO_BLUETOOTH`.
- Raspberry Pi OS Trixie no longer uses `hciuart`; the kernel binds the chip
  via device tree. Its absence is not an error.
- `ha-ef-ble` has dependencies not listed in `manifest.json`:
  `bleak-retry-connector`, `aiohttp`.
- There is no `ef-ble` package on PyPI. The protocol library lives inside the
  repo under `eflib/` and is imported by path.
- EcoFlow devices accept **one BLE connection at a time**. Close the phone app.
- The station must be awake to advertise. Press the power button before scanning.
- `remaining_time_charging` returns 12927 as a placeholder for "unknown".
- On the Delta 3 Max Plus the USB ports cannot be switched and there is no
  second USB-A port, although the library exposes both.
- The station reports only what leaves its ports. Its own idle draw (BMS,
  inverter standby, display) is not in `output_power`: measured over 13.5 h,
  the counter showed 47 Wh while the state of charge dropped by about 100 Wh.
  Name the Home Assistant sensor "output energy", not "discharge".
- Nodes on other channels are filtered out before logging. Without that filter
  a nearby public mesh fills the journal with discarded-message warnings.

## Credits

Protocol work by [rabits/ha-ef-ble](https://github.com/rabits/ha-ef-ble) and
[ef-ble-reverse](https://github.com/rabits/ef-ble-reverse).
Tested against commit `7fe5589`.

## License

MIT
