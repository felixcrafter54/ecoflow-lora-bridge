#!/usr/bin/env python3
"""Haus: Meshtastic -> MQTT mit Home-Assistant-Autodiscovery."""

import json
import logging
import os
import sys
import time
from pathlib import Path

from dotenv import load_dotenv
import paho.mqtt.client as mqtt
from pubsub import pub
import meshtastic.serial_interface

ROOT = Path(__file__).resolve().parent.parent
load_dotenv(ROOT / ".env")
sys.path.insert(0, str(Path(__file__).resolve().parent))

import protocol as proto

MESH_PORT = os.environ["MESH_PORT"]
CHANNEL   = int(os.environ.get("MESH_CHANNEL_INDEX", 1))
PEER      = int(os.environ["PEER_NODE_ID"], 0)
BROKER    = os.environ.get("MQTT_HOST", "127.0.0.1")
MQTT_PORT = int(os.environ.get("MQTT_PORT", 1883))
MQTT_USER = os.environ.get("MQTT_USER") or None
MQTT_PASS = os.environ.get("MQTT_PASS") or None
BASE      = os.environ.get("MQTT_BASE", "ecoflow")

EXPIRE_AFTER = int(os.environ.get("EXPIRE_AFTER", 1800))

logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s %(levelname)-7s %(message)s")
log = logging.getLogger("gateway")

DEVICE = {
    "identifiers": ["ef_garage"],
    "name": "EcoFlow Garage",
    "manufacturer": "EcoFlow",
    "model": "Delta 3 Max Plus",
}

# key, Anzeigename, Einheit, device_class, state_class
SENSOREN = [
    ("soc",     "Ladestand",        "%",  "battery",     "measurement"),
    ("p_in",    "Eingangsleistung", "W",  "power",       "measurement"),
    ("p_out",   "Ausgangsleistung", "W",  "power",       "measurement"),
    ("p_pv1",   "Solar 1",          "W",  "power",       "measurement"),
    ("p_pv2",   "Solar 2",          "W",  "power",       "measurement"),
    ("p_dc12",  "12 V Ausgang",     "W",  "power",       "measurement"),
    ("p_usba",  "USB-A",            "W",  "power",       "measurement"),
    ("p_usbc",  "USB-C 1",          "W",  "power",       "measurement"),
    ("p_usbc2", "USB-C 2",          "W",  "power",       "measurement"),
    ("p_usbc3", "USB-C 3",          "W",  "power",       "measurement"),
    ("temp",    "Zelltemperatur",   "°C", "temperature", "measurement"),
    ("rt_dis",  "Restlaufzeit",     "min", "duration",   "measurement"),
    ("rt_chg",  "Restladezeit",     "min", "duration",   "measurement"),
]

# Bewusst "Ausgangsenergie", nicht "Entladung":
# der Eigenverbrauch der Station steckt nicht in output_power.
ENERGIE = [
    ("wh_in",  "Eingangsenergie"),
    ("wh_out", "Ausgangsenergie"),
    ("wh_pv1", "Solarertrag 1"),
    ("wh_pv2", "Solarertrag 2"),
]

# Flag -> (Anzeigename, Kommandokuerzel oder None)
SCHALTER = [
    ("ac_ports",   "AC-Ausgang",    "ac"),
    ("ac_ports_2", "AC-Ausgang 2",  "ac2"),
    ("dc_12v_port", "12-V-Ausgang", "dc"),
]

LINK = [("rssi", "Signalstärke", "dBm"), ("snr", "Rauschabstand", "dB")]


class Gateway:
    def __init__(self):
        self.mesh = None
        self.last_seq = None
        self.mc = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2,
                              client_id="ecoflow-gateway")
        if MQTT_USER:
            self.mc.username_pw_set(MQTT_USER, MQTT_PASS)
        self.mc.will_set(f"{BASE}/status", "offline", retain=True)
        self.mc.on_connect = self.on_mqtt_connect
        self.mc.on_message = self.on_mqtt_message

    # --- Discovery ----------------------------------------------------
    def publish_discovery(self):
        def cfg(komponente, uid, payload):
            payload.update({"unique_id": uid, "device": DEVICE,
                            "availability_topic": f"{BASE}/status"})
            self.mc.publish(f"homeassistant/{komponente}/{uid}/config",
                            json.dumps(payload), retain=True)

        for key, name, unit, dclass, sclass in SENSOREN:
            cfg("sensor", f"ef_{key}", {
                "name": name,
                "state_topic": f"{BASE}/state/{key}",
                "unit_of_measurement": unit,
                "device_class": dclass,
                "state_class": sclass,
                "expire_after": EXPIRE_AFTER,
            })

        for key, name in ENERGIE:
            cfg("sensor", f"ef_{key}", {
                "name": name,
                "state_topic": f"{BASE}/state/{key}",
                "unit_of_measurement": "kWh",
                "device_class": "energy",
                "state_class": "total_increasing",
                "suggested_display_precision": 2,
            })

        for key, name, unit in LINK:
            cfg("sensor", f"ef_link_{key}", {
                "name": name,
                "state_topic": f"{BASE}/link/{key}",
                "unit_of_measurement": unit,
                "device_class": "signal_strength" if key == "rssi" else None,
                "state_class": "measurement",
                "entity_category": "diagnostic",
            })

        cfg("sensor", "ef_seq", {
            "name": "Paketzähler",
            "state_topic": f"{BASE}/link/seq",
            "state_class": "total_increasing",
            "entity_category": "diagnostic",
        })

        for key, name, kurz in SCHALTER:
            cfg("switch", f"ef_{key}", {
                "name": name,
                "state_topic": f"{BASE}/state/{key}",
                "command_topic": f"{BASE}/cmd/{kurz}",
                "payload_on": "1", "payload_off": "0",
                "optimistic": False, "retain": False,
                "expire_after": EXPIRE_AFTER,
            })

        cfg("binary_sensor", "ef_error", {
            "name": "Störung",
            "state_topic": f"{BASE}/state/error_occurred",
            "payload_on": "1", "payload_off": "0",
            "device_class": "problem",
        })

        log.info("Discovery gesendet")

    # --- MQTT ---------------------------------------------------------
    def on_mqtt_connect(self, client, userdata, flags, rc, props=None):
        log.info("MQTT verbunden (rc=%s)", rc)
        client.subscribe(f"{BASE}/cmd/#")
        self.publish_discovery()

    def on_mqtt_message(self, client, userdata, msg):
        kurz = msg.topic.rsplit("/", 1)[-1]
        wert = msg.payload.decode().strip()
        if kurz not in proto.COMMANDS:
            log.warning("unbekanntes Kommando-Topic: %s", msg.topic)
            return
        if wert.upper() in ("ON", "OFF"):
            wert = "1" if wert.upper() == "ON" else "0"
        text = f"{kurz} {wert}".strip() if wert else kurz
        try:
            proto.decode_cmd(text)
        except proto.ProtocolError as e:
            log.warning("Kommando verworfen: %s", e)
            return
        log.info("tx cmd %r", text)
        self.mesh.sendText(text, destinationId=PEER,
                           channelIndex=CHANNEL, wantAck=True)

    # --- Mesh ---------------------------------------------------------
    def on_mesh(self, packet, interface):
        d = packet.get("decoded", {})
        if d.get("portnum") != "TEXT_MESSAGE_APP":
            return
        if packet.get("from") != PEER:
            return   		# fremder Node, still ignorieren

        try:
            s = proto.decode(d.get("text", ""))
        except proto.ProtocolError as e:
            log.warning("Paket verworfen: %s", e)
            return

        if self.last_seq is not None:
            fehlend = (s.seq - self.last_seq - 1) % 65536
            if 0 < fehlend < 1000:
                log.warning("%d Paket(e) fehlen", fehlend)
        self.last_seq = s.seq

        pub_ = lambda t, v: self.mc.publish(f"{BASE}/{t}", v, retain=True)

        for key, *_ in SENSOREN:
            v = getattr(s, key)
            if key == "rt_chg":
                v = s.rt_chg_clean
            elif key == "rt_dis":
                v = s.rt_dis_clean
            pub_(f"state/{key}", "None" if v is None else v)

        for key, _ in ENERGIE:
            v = getattr(s, key)
            pub_(f"state/{key}", "None" if v is None else round(v / 1000, 3))

        for flag in proto.FLAG_BITS:
            pub_(f"state/{flag}", "1" if s.get_flag(flag) else "0")

        pub_("link/rssi", packet.get("rxRssi", ""))
        pub_("link/snr", packet.get("rxSnr", ""))
        pub_("link/seq", s.seq)
        pub_("status", "online")

        log.info("rx seq=%d soc=%s rssi=%s", s.seq, s.soc, packet.get("rxRssi"))

    # --- Start --------------------------------------------------------
    def run(self):
        pub.subscribe(self.on_mesh, "meshtastic.receive")
        self.mesh = meshtastic.serial_interface.SerialInterface(MESH_PORT)
        info = self.mesh.getMyNodeInfo()
        log.info("Node: %s (%#010x) <- Peer %#010x",
                 info["user"]["longName"], info["num"], PEER)

        self.mc.connect(BROKER, MQTT_PORT, 60)
        self.mc.loop_start()
        try:
            while True:
                time.sleep(1)
        except KeyboardInterrupt:
            pass
        finally:
            self.mc.publish(f"{BASE}/status", "offline", retain=True)
            time.sleep(0.5)
            self.mc.loop_stop()
            self.mesh.close()


if __name__ == "__main__":
    Gateway().run()
