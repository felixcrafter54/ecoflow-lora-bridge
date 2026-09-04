#!/usr/bin/env python3
"""EcoFlow Delta 3 Max Plus - interaktives Steuern."""

import asyncio, inspect, logging, os, sys

REPO = "/home/felix/ha-ef-ble/custom_components/ef_ble"
sys.path.insert(0, REPO)
sys.path.insert(0, REPO + "/eflib/pb")

from bleak import BleakScanner
import eflib

MAC     = os.environ.get("EF_MAC", "A0:F2:62:4B:EF:6E")
USER_ID = os.environ["EF_USER_ID"]

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)-7s %(message)s")
logging.getLogger("eflib").setLevel(logging.INFO)

STATUS_FIELDS = ("battery_level", "input_power", "output_power", "ac_ports",
                 "ac_ports_2", "dc_12v_port", "usb_ports", "ac_charging_speed",
                 "battery_charge_limit_min", "battery_charge_limit_max")


async def find(mac, timeout=20):
    found, evt = None, asyncio.Event()
    def cb(d, a):
        nonlocal found
        if d.address.upper() == mac.upper():
            found = (d, a); evt.set()
    sc = BleakScanner(detection_callback=cb)
    await sc.start()
    try: await asyncio.wait_for(evt.wait(), timeout)
    except asyncio.TimeoutError: pass
    finally: await sc.stop()
    return found


def list_controls(dev):
    """Alle aufrufbaren Coroutinen des Geraets finden."""
    skip = {"connect", "disconnect", "data_parse", "packet_parse", "send_packet"}
    out = {}
    for cls in type(dev).__mro__:
        for name, attr in vars(cls).items():
            if name.startswith("_") or name in out or name in skip:
                continue
            if inspect.iscoroutinefunction(attr):
                params = [p for p in inspect.signature(attr).parameters if p != "self"]
                out[name] = params
    return out


def show_status(dev):
    for f in STATUS_FIELDS:
        print(f"  {f:26} = {getattr(dev, f, '-')}")


def to_val(a: str):
    s = a.strip().lower()
    if s in ("true", "on", "1", "yes", "ein", "an"): return True
    if s in ("false", "off", "0", "no", "aus"):      return False
    try:
        return int(a)
    except ValueError:
        raise ValueError(f"unklarer Wert: {a!r} - nutze on/off oder eine Zahl")


async def main():
    found = await find(MAC)
    if not found:
        print("nicht gefunden - Station aufwecken"); return

    dev = eflib.NewDevice(*found)
    await dev.connect(user_id=USER_ID, max_attempts=3)
    print("verbunden, warte auf ersten Datensatz...")
    await asyncio.sleep(8)

    ctl = list_controls(dev)
    print("\n=== Methoden am Geraet ===")
    for name, params in sorted(ctl.items()):
        print(f"  {name}({', '.join(params)})")
    print("\nBeispiel: enable_ac_ports off     Status: ?     Ende: q\n")

    loop = asyncio.get_running_loop()
    try:
        while True:
            line = await loop.run_in_executor(None, input, "> ")
            line = "".join(c for c in line if c.isprintable()).strip()
            if not line:
                continue
            if line == "q":
                break
            if line == "?":
                show_status(dev); continue

            parts = line.split()
            name, args = parts[0], parts[1:]

            if name not in ctl:
                print(f"  unbekannt: {name!r}"); continue
            if name == "power_off":
                print("  blockiert - danach kein BLE-Zugriff mehr"); continue
            if len(args) != len(ctl[name]):
                print(f"  braucht {len(ctl[name])} Argument(e): "
                      f"{name}({', '.join(ctl[name])})")
                continue

            try:
                await getattr(dev, name)(*[to_val(a) for a in args])
                print("  gesendet")
            except (ValueError, TypeError) as e:
                print(f"  {e}")
            except Exception as e:
                print(f"  Fehler: {e!r}")
    finally:
        print("trenne...")
        await dev.disconnect()


if __name__ == "__main__":
    try: asyncio.run(main())
    except KeyboardInterrupt: print("\nabgebrochen")
