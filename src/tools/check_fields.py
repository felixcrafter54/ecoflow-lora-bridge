#!/usr/bin/env python3
"""Prüft nach einem ha-ef-ble-Update, ob alle benutzten Felder
und Methoden am Gerät noch existieren. Braucht eine BLE-Verbindung."""

import asyncio, os, sys
from pathlib import Path
from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parents[2]
load_dotenv(ROOT / ".env")
V = ROOT / "vendor/ha-ef-ble/custom_components/ef_ble"
sys.path.insert(0, str(V)); sys.path.insert(0, str(V / "eflib/pb"))
sys.path.insert(0, str(ROOT / "src"))

from bleak import BleakScanner
import eflib
import protocol as proto

sys.path.insert(0, str(ROOT / "src"))
from bridge import FIELD_MAP

MAC, UID = os.environ["EF_MAC"], os.environ["EF_USER_ID"]


async def main():
    found, evt = None, asyncio.Event()
    def cb(d, a):
        nonlocal found
        if d.address.upper() == MAC.upper():
            found = (d, a); evt.set()
    sc = BleakScanner(detection_callback=cb)
    await sc.start()
    try: await asyncio.wait_for(evt.wait(), 30)
    except asyncio.TimeoutError: pass
    finally: await sc.stop()

    if not found:
        print("Station nicht gefunden"); return 1

    dev = eflib.NewDevice(*found)
    print(f"Gerät: {type(dev).__module__}")
    await dev.connect(user_id=UID)
    await asyncio.sleep(10)

    fehler = 0
    print("\nFelder:")
    for own, remote in sorted(FIELD_MAP.items()):
        da = hasattr(dev, remote)
        wert = getattr(dev, remote, None)
        marke = "ok " if da else "FEHLT"
        if not da: fehler += 1
        print(f"  {marke}  {own:9} -> {remote:28} = {wert}")

    print("\nFlags:")
    for flag in proto.FLAG_BITS:
        da = hasattr(dev, flag)
        if not da: fehler += 1
        print(f"  {'ok ' if da else 'FEHLT'}  {flag}")

    print("\nKommandos:")
    for kurz, (methode, n) in sorted(proto.COMMANDS.items()):
        if methode is None:
            print(f"  --   {kurz:7} (lokal)"); continue
        da = callable(getattr(dev, methode, None))
        if not da: fehler += 1
        print(f"  {'ok ' if da else 'FEHLT'}  {kurz:7} -> {methode}")

    await dev.disconnect()
    print(f"\n{fehler} Problem(e)")
    return 1 if fehler else 0


sys.exit(asyncio.run(main()))
