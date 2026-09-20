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

## Layout

    src/protocol.py     packet format and command whitelist, used by both ends
    src/storage.py      SQLite buffer and persistent energy counters (garage)
    src/bridge.py       BLE → SQLite → LoRa, and commands back (garage)
    src/gateway.py      LoRa → MQTT with HA auto-discovery (house)
    src/tools/          scan, interactive control, field check, packet decoder
    systemd/            unit files for both ends
    vendor/ha-ef-ble    pinned protocol library, cloned by setup.sh

## Hardware

- Raspberry Pi 3B (or newer) with Raspberry Pi OS Lite
- EcoFlow Delta 3 Max Plus (SN prefix `D3M1`)
- 2× Meshtastic node, EU_868 — e.g. Seeed XIAO ESP32S3 + Wio-SX1262
- USB SSD for the database (optional but recommended)
- An LXC container or any small machine at the house end

## Installation

The same repository runs on both ends. `setup.sh` takes the role as its
argument and installs only what that side needs.

### Garage (Raspberry Pi)

    git clone https://github.com/felixcrafter54/ecoflow-lora-bridge
    cd ecoflow-lora-bridge
    ./setup.sh garage
    nano .env          # EF_MAC, EF_USER_ID, MESH_PORT, PEER_NODE_ID

Find the station and read its serial number:

    .venv/bin/python src/tools/scan.py

Verify the connection and try the controls by hand:

    .venv/bin/python src/tools/ctl.py

Allow the service to restart the machine:

    sudo visudo -f /etc/sudoers.d/ecoflow
    # felix ALL=(ALL) NOPASSWD: /usr/bin/systemctl reboot, /usr/bin/systemctl poweroff

Install the service:

    sudo cp systemd/ef-bridge.service /etc/systemd/system/
    sudo systemctl daemon-reload
    sudo systemctl enable --now ef-bridge
    journalctl -u ef-bridge -f

### House (LXC container)

Pass the USB node into the container first — on the Proxmox host, in
`/etc/pve/lxc/<VMID>.conf`:

    lxc.cgroup2.devices.allow: c 166:* rwm
    lxc.mount.entry: /dev/serial/by-id/usb-YOUR_NODE dev/mesh none bind,optional,create=file

Major number 166 is USB CDC-ACM; use 188 if the node appears as `ttyUSB`.
Check with `ls -l /dev/ttyACM0` on the host. Restart the container afterwards.

Then inside the container:

    git clone https://github.com/felixcrafter54/ecoflow-lora-bridge /opt/mesh-gateway
    cd /opt/mesh-gateway
    ./setup.sh haus
    nano .env          # MESH_PORT=/dev/mesh, PEER_NODE_ID, MQTT_*

    sudo cp systemd/ef-gateway.service /etc/systemd/system/
    sudo systemctl daemon-reload
    sudo systemctl enable --now ef-gateway

Watch the topics arrive before looking in Home Assistant:

    mosquitto_sub -h $MQTT_HOST -t 'ecoflow/#' -v

### Meshtastic nodes

Both nodes need the same region, the same modem preset and the same channel
PSK. Set up the first, then copy its channel URL to the second:

    P=/dev/serial/by-id/usb-...
    meshtastic --port $P --set lora.region EU_868
    meshtastic --port $P --set lora.modem_preset SHORT_SLOW
    meshtastic --port $P --set-owner "Garage"
    meshtastic --port $P --ch-add ecoflow
    meshtastic --port $P --ch-set psk random --ch-index <n>
    meshtastic --port $P --info            # channel URL and node ID

    meshtastic --port $P2 --seturl "https://meshtastic.org/e/#..."

`--seturl` replaces the whole channel configuration, which is fine on a fresh
node.

The channel **index** may differ between the two nodes depending on the order
channels were created — here it is 2 on the Pi and 1 on the gateway. What has
to match is the PSK. Put each node's own local index in its own `.env`.

Each `.env` holds the *other* node's ID in `PEER_NODE_ID`. Read it from
`myNodeNum`, or from the node list as `!a1d3e080` — drop the exclamation mark
and prefix `0x`.

## Configuration

See `.env.example`; every variable is documented there. The channel PSK is
**not** stored in the repository — it lives on the node itself.

## Commands (house → garage)

Sent as a Meshtastic direct message to the garage node, or published to
`ecoflow/cmd/<command>` on MQTT:

| command    | effect                      |
|------------|-----------------------------|
| `ac 0/1`   | AC output                   |
| `ac2 0/1`  | second AC output            |
| `dc 0/1`   | 12 V output                 |
| `chg <W>`  | AC charging power           |
| `lmin <%>` | discharge limit             |
| `lmax <%>` | charge limit                |
| `ping`     | request an immediate packet |
| `reboot`   | restart the Pi              |

Commands are whitelisted and only accepted from the node configured as
`PEER_NODE_ID`, on the configured channel. After a command the bridge waits
for the station to confirm over BLE and then sends a data packet immediately,
so the switch in Home Assistant only flips once the change actually happened.

## Reading the data

The SQLite database on the Pi holds the raw samples; only the counters are
kept as a single row.

    sqlite3 /mnt/ssd/ecoflow.db
    SELECT wh_in, wh_out, wh_pv1, wh_pv2, seq FROM counters;
    SELECT datetime(ts,'unixepoch','localtime'), soc, p_in, p_out
      FROM samples ORDER BY ts DESC LIMIT 20;

Take a consistent copy rather than copying the file (WAL mode):

    sqlite3 /mnt/ssd/ecoflow.db ".backup /tmp/ecoflow-copy.db"

`src/tools/ecoflow-paket-decoder.html` opens in any browser and decodes
packets pasted from the Meshtastic app or from the log — including several at
once, with gap detection over the sequence number. Useful when there is no
gateway running yet.

## Updating ha-ef-ble

The protocol library is reverse-engineered and pinned to a tested commit in
`setup.sh`. To move to a newer one:

1. Read the upstream changelog for anything touching the Delta 3 family.
2. Bump `EF_BLE_COMMIT` in `setup.sh` and re-run `./setup.sh garage`.
3. Check that nothing the bridge relies on was renamed:

       .venv/bin/python src/tools/check_fields.py

   It reports every field in `FIELD_MAP`, every flag, and every control method
   in `COMMANDS`, and exits non-zero if any are missing.

4. Watch for new dependencies. `manifest.json` does not list all of them — if
   an import fails, install the named module and add it to `requirements.txt`.
5. Restart the service and watch one heartbeat go out.

Roll back by restoring the old hash and re-running `setup.sh`.

## Changing the packet format

`src/protocol.py` is imported by both ends, so the format cannot drift. When
changing `LAYOUT` or `FLAG_BITS`, increment `VERSION` — the receiver discards
packets it does not recognise instead of misreading them. Update both ends
before restarting either, and mirror the change in
`src/tools/ecoflow-paket-decoder.html`.

Existing bit positions should be left alone once both ends are in service; free
a bit rather than renumbering.

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
  The same is true of the Meshtastic node: the app and the service cannot both
  be connected.
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
- Total idle draw of the whole setup is around 7.5 W, of which roughly 3.5 W is
  the Pi and the node. That is about 9 % of the battery per day with no sun.
- Direct messages with PKI encryption do not carry the channel index you would
  expect on the receiving node. Filter incoming packets by sender, not by
  channel; `MESH_CHANNEL_INDEX` only applies when sending.

## Credits

Protocol work by [rabits/ha-ef-ble](https://github.com/rabits/ha-ef-ble) and
[ef-ble-reverse](https://github.com/rabits/ef-ble-reverse).
Tested against commit `affbb60`.

## License

MIT
