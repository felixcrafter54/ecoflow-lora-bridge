#!/usr/bin/env python3
"""SQLite-Puffer und Energiezaehler fuer die Garagenseite."""

import logging
import os
import sqlite3
import time
from dataclasses import asdict

log = logging.getLogger("storage")

# Groesste Luecke zwischen zwei Messwerten, ueber die noch integriert wird.
# Darueber gilt: lieber Energie fehlt, als dass sie erfunden wird.
MAX_GAP_S = 300

SCHEMA = """
PRAGMA journal_mode=WAL;
PRAGMA synchronous=NORMAL;

CREATE TABLE IF NOT EXISTS samples (
    ts       INTEGER PRIMARY KEY,
    soc      REAL, p_in REAL, p_out REAL, p_pv1 REAL, p_pv2 REAL,
    p_dc12   REAL, p_usba REAL, p_usbc REAL, p_usbc2 REAL, p_usbc3 REAL,
    temp     REAL, flags INTEGER,
    rt_chg   INTEGER, rt_dis INTEGER
);

CREATE TABLE IF NOT EXISTS counters (
    id      INTEGER PRIMARY KEY CHECK (id = 1),
    wh_in   REAL NOT NULL DEFAULT 0,
    wh_out  REAL NOT NULL DEFAULT 0,
    wh_pv1  REAL NOT NULL DEFAULT 0,
    wh_pv2  REAL NOT NULL DEFAULT 0,
    seq     INTEGER NOT NULL DEFAULT 0,
    updated INTEGER
);

CREATE TABLE IF NOT EXISTS events (
    ts   INTEGER,
    kind TEXT,
    text TEXT
);

INSERT OR IGNORE INTO counters (id) VALUES (1);
"""

SAMPLE_COLS = ["soc", "p_in", "p_out", "p_pv1", "p_pv2", "p_dc12",
               "p_usba", "p_usbc", "p_usbc2", "p_usbc3",
               "temp", "flags", "rt_chg", "rt_dis"]


class Storage:
    def __init__(self, path: str, flush_interval: float = 60.0,
                 retention_days: int = 0):
        os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
        self.db = sqlite3.connect(path, isolation_level=None)
        self.db.executescript(SCHEMA)
        self.flush_interval = flush_interval
        self.retention_days = retention_days

        self._buf: list[tuple] = []
        self._last_flush = time.monotonic()
        self._last_cleanup = 0.0

        # Integrationszustand
        self._t_prev: float | None = None
        self._p_prev = (0.0, 0.0, 0.0, 0.0)

        row = self.db.execute(
            "SELECT wh_in, wh_out, wh_pv1, wh_pv2, seq FROM counters WHERE id=1"
        ).fetchone()
        self.wh_in, self.wh_out, self.wh_pv1, self.wh_pv2, self.seq = row
        log.info("Zaehler geladen: in=%.1f out=%.1f pv1=%.1f pv2=%.1f seq=%d",
                 self.wh_in, self.wh_out, self.wh_pv1, self.wh_pv2, self.seq)

    # --- Energie ------------------------------------------------------
    def integrate(self, p_in, p_out, p_pv1, p_pv2, now: float | None = None):
        """Trapezregel ueber den tatsaechlichen Zeitabstand.

        Bei jedem BLE-Push aufrufen, nicht auf einem Timer.
        """
        now = now if now is not None else time.monotonic()
        p = tuple(float(v or 0.0) for v in (p_in, p_out, p_pv1, p_pv2))

        if self._t_prev is not None:
            dt = now - self._t_prev
            if 0 < dt <= MAX_GAP_S:
                f = dt / 3600.0
                self.wh_in  += (self._p_prev[0] + p[0]) / 2 * f
                self.wh_out += (self._p_prev[1] + p[1]) / 2 * f
                self.wh_pv1 += (self._p_prev[2] + p[2]) / 2 * f
                self.wh_pv2 += (self._p_prev[3] + p[3]) / 2 * f
            elif dt > MAX_GAP_S:
                log.warning("Luecke %.0fs - nicht integriert", dt)
                self.event("gap", f"{dt:.0f}s")

        self._t_prev, self._p_prev = now, p

    def next_seq(self) -> int:
        self.seq = (self.seq + 1) % 65536
        return self.seq

    def set_counters(self, wh_in=None, wh_out=None, wh_pv1=None, wh_pv2=None):
        """Zaehlerstand von Hand setzen (z.B. nach Neuaufsetzen)."""
        for name, val in (("wh_in", wh_in), ("wh_out", wh_out),
                          ("wh_pv1", wh_pv1), ("wh_pv2", wh_pv2)):
            if val is not None:
                setattr(self, name, float(val))
        self.flush(force=True)
        log.warning("Zaehler manuell gesetzt")

    # --- Messwerte ----------------------------------------------------
    def add(self, sample, ts: int | None = None):
        """Sample (aus protocol.py) in den Puffer legen."""
        ts = ts if ts is not None else int(time.time())
        d = asdict(sample)
        self._buf.append((ts, *(d.get(c) for c in SAMPLE_COLS)))
        if time.monotonic() - self._last_flush >= self.flush_interval:
            self.flush()

    def event(self, kind: str, text: str = ""):
        self.db.execute("INSERT INTO events (ts, kind, text) VALUES (?,?,?)",
                        (int(time.time()), kind, text))

    def flush(self, force: bool = False):
        """Puffer und Zaehler schreiben. Vor dem Shutdown zwingend aufrufen."""
        if not self._buf and not force:
            self._write_counters()
            self._last_flush = time.monotonic()
            return
        try:
            self.db.execute("BEGIN")
            if self._buf:
                self.db.executemany(
                    f"INSERT OR REPLACE INTO samples "
                    f"(ts,{','.join(SAMPLE_COLS)}) "
                    f"VALUES ({','.join('?' * (len(SAMPLE_COLS) + 1))})",
                    self._buf)
            self._write_counters(in_tx=True)
            self.db.execute("COMMIT")
            log.debug("%d Datensaetze geschrieben", len(self._buf))
            self._buf.clear()
        except sqlite3.Error as e:
            log.error("DB-Fehler: %s", e)
            try: self.db.execute("ROLLBACK")
            except sqlite3.Error: pass
        self._last_flush = time.monotonic()
        self._maybe_cleanup()

    def _write_counters(self, in_tx: bool = False):
        self.db.execute(
            "UPDATE counters SET wh_in=?, wh_out=?, wh_pv1=?, wh_pv2=?, "
            "seq=?, updated=? WHERE id=1",
            (self.wh_in, self.wh_out, self.wh_pv1, self.wh_pv2,
             self.seq, int(time.time())))

    def _maybe_cleanup(self):
        if self.retention_days <= 0:
            return
        if time.monotonic() - self._last_cleanup < 86400:
            return
        cutoff = int(time.time()) - self.retention_days * 86400
        n = self.db.execute("DELETE FROM samples WHERE ts < ?",
                            (cutoff,)).rowcount
        if n:
            log.info("%d alte Datensaetze geloescht", n)
        self._last_cleanup = time.monotonic()

    def close(self):
        self.flush(force=True)
        self.db.close()


if __name__ == "__main__":
    import sys
    sys.path.insert(0, os.path.dirname(__file__))
    from protocol import Sample

    logging.basicConfig(level=logging.DEBUG, format="%(levelname)s %(message)s")
    st = Storage("/tmp/test.db", flush_interval=0)

    start_in = st.wh_in
    # 100 W ueber 36 s -> 1 Wh
    st.integrate(100, 0, 0, 0, now=0.0)
    st.integrate(100, 0, 0, 0, now=36.0)
    print(f"wh_in: {start_in:.3f} -> {st.wh_in:.3f}  (erwartet +1.000)")

    # Luecke wird uebersprungen
    st.integrate(100, 0, 0, 0, now=36.0 + MAX_GAP_S + 10)
    print(f"nach Luecke: {st.wh_in:.3f}  (unveraendert)")

    s = Sample(soc=78.0, p_in=0, p_out=16, temp=22, seq=st.next_seq())
    st.add(s)
    st.flush(force=True)
    print("Zeilen:", st.db.execute("SELECT COUNT(*) FROM samples").fetchone()[0])
    st.close()
