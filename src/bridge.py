#!/usr/bin/env python3
"""Garage: EcoFlow BLE -> SQLite -> Meshtastic. Und Kommandos zurueck.

Drei entkoppelte Takte:
  Integration ~1s (jedes BLE-Paket)  - volle Genauigkeit fuer Wh
  SQLite      bei Aenderung, sonst 30s
  LoRa        fruehestens MIN_GAP, spaetestens HEARTBEAT
"""

import asyncio
import logging
import os
import subprocess
import sys
import time
from pathlib import Path

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent.parent
load_dotenv(ROOT / ".env")

VENDOR = ROOT / "vendor/ha-ef-ble/custom_components/ef_ble"
sys.path.insert(0, str(VENDOR))
sys.path.insert(0, str(VENDOR / "eflib/pb"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from bleak import BleakScanner
import eflib
import meshtastic.serial_interface
from pubsub import pub

import protocol as proto
from protocol import Sample
from storage import Storage

# --- Konfiguration ---------------------------------------------------
MAC        = os.environ["EF_MAC"]
USER_ID    = os.environ["EF_USER_ID"]
MESH_PORT  = os.environ["MESH_PORT"]
CHANNEL    = int(os.environ.get("MESH_CHANNEL_INDEX", 1))
PEER       = int(os.environ["PEER_NODE_ID"], 0)
MIN_GAP    = float(os.environ.get("MIN_GAP", 60))
HEARTBEAT  = float(os.environ.get("HEARTBEAT", 300))
SHUT_SOC   = float(os.environ.get("SHUTDOWN_SOC", 0))
SHUT_HOLD  = float(os.environ.get("SHUTDOWN_HOLD", 300))
DB_PATH    = os.environ.get("DB_PATH", "/mnt/ssd/ecoflow.db")
RETENTION  = int(os.environ.get("RETENTION_DAYS", 0))

ROW_INTERVAL = 30.0     # SQLite: spaetestens alle 30s eine Zeile
DATA_TIMEOUT = 600.0    # ohne BLE-Daten: Prozess beenden

# Sofortpaket erst ab echtem Lastwechsel (W)
DELTA_W = {"p_in": 150, "p_out": 150, "p_pv1": 150, "p_pv2": 150}
DELTA_SOC = 1.0

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)-7s %(name)s: %(message)s")
logging.getLogger("eflib").setLevel(logging.INFO)
log = logging.getLogger("bridge")

# Sample-Feld -> Geraete-Attribut
FIELD_MAP = {
    "soc":     "battery_level",
    "p_in":    "input_power",
    "p_out":   "output_power",
    "p_pv1":   "solar_input_power",
    "p_pv2":   "solar_input_power_2",
    "p_dc12":  "dc12v_output_power",
    "p_usba":  "usba_output_power",
    "p_usbc":  "usbc_output_power",
    "p_usbc2": "usbc2_output_power",
    "p_usbc3": "usbc3_output_power",
    "temp":    "cell_temperature",
    "rt_chg":  "remaining_time_charging",
    "rt_dis":  "remaining_time_discharging",
}


def read_sample(dev) -> Sample:
    s = Sample()
    for own, remote in FIELD_MAP.items():
        setattr(s, own, getattr(dev, remote, None))
    for flag in proto.FLAG_BITS:
        s.set_flag(flag, bool(getattr(dev, flag, False)))
    return s


def should_send(cur: Sample, prev: Sample | None, last_tx: float) -> str | None:
    dt = time.monotonic() - last_tx
    if dt < MIN_GAP:
        return None
    if prev is None:
        return "erstes Paket"
    if cur.flags != prev.flags:
        return "Flags"
    if cur.soc is not None and prev.soc is not None \
            and abs(cur.soc - prev.soc) >= DELTA_SOC:
        return "SoC"
    for name, thr in DELTA_W.items():
        a, b = getattr(cur, name), getattr(prev, name)
        if a is not None and b is not None and abs(a - b) > thr:
            return name
    if dt >= HEARTBEAT:
        return "heartbeat"
    return None


class Bridge:
    def __init__(self):
        self.st = Storage(DB_PATH, retention_days=RETENTION)
        self.dev = None
        self.mesh = None
        self.loop = None
        self.cur: Sample | None = None
        self.prev: Sample | None = None
        self.last_tx = 0.0
        self.last_row = 0.0
        self.last_data = time.monotonic()
        self.low_since: float | None = None
        self.force_send = asyncio.Event()
        self.stopping = False

    # --- BLE ---------------------------------------------------------
    async def find(self, timeout=30):
        found, evt = None, asyncio.Event()

        def cb(d, a):
            nonlocal found
            if d.address.upper() == MAC.upper():
                found = (d, a)
                evt.set()

        sc = BleakScanner(detection_callback=cb)
        await sc.start()
        try:
            await asyncio.wait_for(evt.wait(), timeout)
        except asyncio.TimeoutError:
            pass
        finally:
            await sc.stop()
        return found

    async def ble_task(self):
        backoff = 5
        while not self.stopping:
            try:
                found = await self.find()
                if not found:
                    log.warning("Station nicht gefunden")
                    await asyncio.sleep(backoff)
                    backoff = min(backoff * 2, 300)
                    continue

                self.dev = eflib.NewDevice(*found)
                if self.dev is None or eflib.is_unsupported(self.dev):
                    log.error("Geraet nicht unterstuetzt")
                    await asyncio.sleep(300)
                    continue

                log.info("verbinde: %s", type(self.dev).__module__)
                await self.dev.connect(user_id=USER_ID)
                self.dev.on_packet_parsed(self.on_packet)
                self.last_data = time.monotonic()
                backoff = 5
                self.st.event("ble", "connected")

                while not self.stopping:
                    await asyncio.sleep(30)
                    conn = getattr(self.dev, "_conn", None)
                    if conn is not None and not getattr(conn, "is_connected", True):
                        log.warning("BLE getrennt")
                        break
            except Exception as e:
                log.warning("BLE-Fehler: %r", e)
                self.st.event("ble", f"error: {e!r}")
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, 300)

    # --- pro BLE-Paket (~1s) -----------------------------------------
    def on_packet(self, packet):
        if self.dev is None or self.stopping:
            return
        s = read_sample(self.dev)
        if s.soc is None:
            return

        now = time.monotonic()
        self.last_data = now

        # Energie immer integrieren - unabhaengig vom Sendetakt
        self.st.integrate(s.p_in, s.p_out, s.p_pv1, s.p_pv2, now)
        s.wh_in  = round(self.st.wh_in)
        s.wh_out = round(self.st.wh_out)
        s.wh_pv1 = round(self.st.wh_pv1)
        s.wh_pv2 = round(self.st.wh_pv2)

        changed = bool(self.dev.updated_fields)
        if changed or now - self.last_row >= ROW_INTERVAL:
            self.st.add(s)
            self.last_row = now
        self.dev.reset_updated()

        self.cur = s
        self.check_soc(s)

    def check_soc(self, s: Sample):
        if s.soc is None or SHUT_SOC <= 0:
            return
        now = time.monotonic()
        if s.soc <= SHUT_SOC:
            if self.low_since is None:
                self.low_since = now
                log.warning("SoC %.0f%% unter Schwelle %.0f%%", s.soc, SHUT_SOC)
            elif now - self.low_since >= SHUT_HOLD:
                self.loop.create_task(self.shutdown())
        else:
            self.low_since = None

    async def shutdown(self):
        if self.stopping:
            return
        self.stopping = True
        log.error("SoC dauerhaft niedrig - fahre herunter")
        self.st.event("shutdown", "low soc")
        await self.send("shutdown")
        await asyncio.sleep(5)
        self.st.close()
        subprocess.run(["sudo", "systemctl", "poweroff"])

    # --- Senden ------------------------------------------------------
    async def send(self, reason: str = ""):
        if self.cur is None or self.mesh is None:
            return
        s = self.cur
        s.seq = self.st.next_seq()
        text = proto.encode(s)
        try:
            await self.loop.run_in_executor(
                None,
                lambda: self.mesh.sendText(text, destinationId=PEER,
                                           channelIndex=CHANNEL, wantAck=True))
            self.last_tx = time.monotonic()
            self.prev = s
            log.info("tx seq=%d (%s) soc=%.0f %dZ",
                     s.seq, reason, s.soc or 0, len(text))
        except Exception as e:
            log.error("Senden fehlgeschlagen: %r", e)

    async def send_task(self):
        while not self.stopping:
            try:
                await asyncio.wait_for(self.force_send.wait(), timeout=2.0)
                self.force_send.clear()
                await self.send("kommando")
                continue
            except asyncio.TimeoutError:
                pass
            if self.cur is None:
                continue
            reason = should_send(self.cur, self.prev, self.last_tx)
            if reason:
                await self.send(reason)

    # --- Empfangen ---------------------------------------------------
    def on_mesh(self, packet, interface):
        """Laeuft im Meshtastic-Thread - nur weiterreichen."""
        d = packet.get("decoded", {})
        if d.get("portnum") != "TEXT_MESSAGE_APP":
            return
        src = packet.get("from")
        if src != PEER:
            log.warning("Nachricht von %#010x verworfen", src or 0)
            return
        text = d.get("text", "")
        self.loop.call_soon_threadsafe(
            lambda: self.loop.create_task(self.handle_cmd(text)))

    async def handle_cmd(self, text: str):
        try:
            name, args = proto.decode_cmd(text)
        except proto.ProtocolError as e:
            log.warning("Kommando abgelehnt: %s", e)
            return

        log.info("rx cmd %s %s", name, args)
        self.st.event("cmd", text)

        if name == "ping":
            self.force_send.set()
            return
        if name == "reboot":
            log.warning("Neustart per Kommando")
            self.st.close()
            await asyncio.sleep(1)
            subprocess.run(["sudo", "systemctl", "reboot"])
            return

        method_name, _ = proto.COMMANDS[name]
        if self.dev is None:
            log.warning("keine BLE-Verbindung")
            return
        fn = getattr(self.dev, method_name, None)
        if fn is None:
            log.error("Methode %s fehlt am Geraet", method_name)
            return
        try:
            await fn(*args)
            log.info("%s ausgefuehrt", method_name)
        except Exception as e:
            log.error("%s fehlgeschlagen: %r", method_name, e)
            return
        await asyncio.sleep(3)      # BLE-Rueckmeldung abwarten
        self.force_send.set()

    # --- Watchdog ----------------------------------------------------
    async def watchdog_task(self):
        while not self.stopping:
            await asyncio.sleep(60)
            age = time.monotonic() - self.last_data
            if age > DATA_TIMEOUT:
                log.error("seit %.0fs keine BLE-Daten - beende", age)
                self.st.event("watchdog", f"{age:.0f}s")
                self.st.close()
                os._exit(1)          # systemd startet neu

    # --- Start -------------------------------------------------------
    async def run(self):
        self.loop = asyncio.get_running_loop()
        pub.subscribe(self.on_mesh, "meshtastic.receive")
        self.mesh = meshtastic.serial_interface.SerialInterface(MESH_PORT)
        info = self.mesh.getMyNodeInfo()
        log.info("Node: %s (%#010x) -> Peer %#010x",
                 info["user"]["longName"], info["num"], PEER)
        log.info("MIN_GAP=%.0fs HEARTBEAT=%.0fs SHUTDOWN_SOC=%.0f%%",
                 MIN_GAP, HEARTBEAT, SHUT_SOC)

        await asyncio.gather(self.ble_task(), self.send_task(),
                             self.watchdog_task())

    def close(self):
        self.stopping = True
        try:
            self.st.close()
        except Exception:
            pass
        if self.mesh:
            try:
                self.mesh.close()
            except Exception:
                pass


def main():
    b = Bridge()
    try:
        asyncio.run(b.run())
    except KeyboardInterrupt:
        log.info("abgebrochen")
    finally:
        b.close()


if __name__ == "__main__":
    main()
