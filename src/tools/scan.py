# ~/scan.py
import asyncio
from bleak import BleakScanner

async def main():
    devs = await BleakScanner.discover(timeout=15, return_adv=True)
    for d, adv in devs.values():
        if d.name and ("EF" in d.name.upper() or "ECOFLOW" in d.name.upper()):
            print(f"{d.address}  {d.name}  RSSI {adv.rssi}")

asyncio.run(main())
