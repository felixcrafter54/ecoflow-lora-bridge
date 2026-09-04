#!/usr/bin/env python3
"""Gemeinsames Paketformat fuer Garage <-> Haus.

Datenpaket (CSV, Version 3):
    3,soc,in,out,pv1,pv2,dc12,ua,uc,uc2,uc3,temp,flags,seq,
      wh_in,wh_out,wh_pv1,wh_pv2,rt_chg,rt_dis

Leere Felder bedeuten "unbekannt" (None).
"""

from dataclasses import dataclass

VERSION = 3

# EcoFlow liefert diesen Wert, wenn keine Restzeit bestimmbar ist
RT_UNKNOWN = 12927

# Bitpositionen im Flags-Feld.
# Beim Aendern VERSION hochzaehlen - sonst liest die Gegenseite falsch.
FLAG_BITS = {
    "ac_ports":       0,
    "ac_ports_2":     1,
    "dc_12v_port":    2,
    "error_occurred": 3,
}

# Reihenfolge im CSV. (name, skalierung)
# skalierung 1 = ganze Einheit, 10 = eine Nachkommastelle
LAYOUT = [
    ("soc",     10),   # %
    ("p_in",     1),   # W
    ("p_out",    1),
    ("p_pv1",    1),
    ("p_pv2",    1),
    ("p_dc12",   1),
    ("p_usba",   1),
    ("p_usbc",   1),
    ("p_usbc2",  1),
    ("p_usbc3",  1),
    ("temp",     1),   # °C
    ("flags",    1),
    ("seq",      1),
    ("wh_in",    1),   # Wh, monoton steigend
    ("wh_out",   1),
    ("wh_pv1",   1),
    ("wh_pv2",   1),
    ("rt_chg",   1),   # min
    ("rt_dis",   1),
]


class ProtocolError(ValueError):
    pass


@dataclass
class Sample:
    soc:      float | None = None
    p_in:     float | None = None
    p_out:    float | None = None
    p_pv1:    float | None = None
    p_pv2:    float | None = None
    p_dc12:   float | None = None
    p_usba:   float | None = None
    p_usbc:   float | None = None
    p_usbc2:  float | None = None
    p_usbc3:  float | None = None
    temp:     float | None = None
    flags:    int = 0
    seq:      int = 0
    wh_in:    int = 0
    wh_out:   int = 0
    wh_pv1:   int = 0
    wh_pv2:   int = 0
    rt_chg:   int | None = None
    rt_dis:   int | None = None

    # --- Flags bequem ansprechen -------------------------------------
    def get_flag(self, name: str) -> bool:
        return bool(self.flags & (1 << FLAG_BITS[name]))

    def set_flag(self, name: str, value: bool) -> None:
        bit = 1 << FLAG_BITS[name]
        self.flags = (self.flags | bit) if value else (self.flags & ~bit)

    @property
    def flag_dict(self) -> dict[str, bool]:
        return {n: self.get_flag(n) for n in FLAG_BITS}

    # --- Restzeiten bereinigt ----------------------------------------
    @property
    def rt_chg_clean(self) -> int | None:
        return _clean_rt(self.rt_chg)

    @property
    def rt_dis_clean(self) -> int | None:
        return _clean_rt(self.rt_dis)


def _clean_rt(v):
    if v is None or v <= 0 or v >= RT_UNKNOWN:
        return None
    return v


def encode(s: Sample) -> str:
    """Sample -> CSV-Zeile."""
    out = [str(VERSION)]
    for name, scale in LAYOUT:
        v = getattr(s, name)
        out.append("" if v is None else str(int(round(v * scale))))
    return ",".join(out)


def decode(text: str) -> Sample:
    """CSV-Zeile -> Sample. Wirft ProtocolError bei Unstimmigkeiten."""
    parts = text.strip().split(",")
    if not parts or not parts[0]:
        raise ProtocolError("leere Nachricht")
    try:
        ver = int(parts[0])
    except ValueError:
        raise ProtocolError(f"kein Datenpaket: {text[:20]!r}") from None
    if ver != VERSION:
        raise ProtocolError(f"Version {ver}, erwarte {VERSION}")
    if len(parts) != len(LAYOUT) + 1:
        raise ProtocolError(f"{len(parts)} Felder, erwarte {len(LAYOUT) + 1}")

    s = Sample()
    for (name, scale), raw in zip(LAYOUT, parts[1:]):
        if raw == "":
            setattr(s, name, None)
            continue
        try:
            v = int(raw)
        except ValueError:
            raise ProtocolError(f"Feld {name}: {raw!r} ist keine Zahl") from None
        setattr(s, name, v / scale if scale != 1 else v)
    return s


# --- Kommandos (Haus -> Garage) --------------------------------------
# Whitelist: kurzer Code -> (eflib-Methode, Argumentanzahl)
COMMANDS = {
    "ac":     ("enable_ac_ports",           1),
    "ac2":    ("enable_ac_ports_2",         1),
    "dc":     ("enable_dc_12v_port",        1),
    "chg":    ("set_ac_charging_speed",     1),
    "lmin":   ("set_battery_charge_limit_min", 1),
    "lmax":   ("set_battery_charge_limit_max", 1),
    "reboot": (None, 0),
    "ping":   (None, 0),
}


def encode_cmd(name: str, *args) -> str:
    if name not in COMMANDS:
        raise ProtocolError(f"unbekanntes Kommando: {name!r}")
    return " ".join([name, *(str(a) for a in args)])


def decode_cmd(text: str) -> tuple[str, list[int]]:
    parts = text.strip().split()
    if not parts:
        raise ProtocolError("leeres Kommando")
    name, raw = parts[0], parts[1:]
    if name not in COMMANDS:
        raise ProtocolError(f"unbekanntes Kommando: {name!r}")
    _, n_args = COMMANDS[name]
    if len(raw) != n_args:
        raise ProtocolError(f"{name} braucht {n_args} Argument(e)")
    try:
        args = [int(a) for a in raw]
    except ValueError:
        raise ProtocolError(f"{name}: Argumente muessen Zahlen sein") from None
    return name, args


def is_command(text: str) -> bool:
    return text.strip().split(" ")[0] in COMMANDS


if __name__ == "__main__":
    s = Sample(soc=78.0, p_in=0, p_out=16, p_pv1=0, p_pv2=0, p_dc12=0,
               p_usba=2, p_usbc=0, p_usbc2=0, p_usbc3=14,
               temp=22, seq=41, wh_in=142380, wh_out=98120,
               wh_pv1=45210, wh_pv2=12030,
               rt_chg=RT_UNKNOWN, rt_dis=3716)
    s.set_flag("dc_12v_port", True)

    line = encode(s)
    print(line)
    print(f"{len(line)} Zeichen")
    back = decode(line)
    assert back == s, "roundtrip fehlgeschlagen"
    print("roundtrip ok")
    print("flags:", back.flag_dict)
    print("rt_chg bereinigt:", back.rt_chg_clean)
    print("cmd:", decode_cmd(encode_cmd("chg", 1000)))
