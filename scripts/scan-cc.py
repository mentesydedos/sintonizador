#!/usr/bin/env python3
"""scan-cc.py — Recorre los 14 multiplexes y reporta cuáles emiten Closed Captions.

Para cada multiplex:
  1. Tunea adapter 0 a la primera frecuencia
  2. Espera lock (hasta 3s)
  3. Captura `CAPTURE_SECONDS` segundos vía Demux TSDEMUX_TAP
  4. Corre `ccextractor` con varias combinaciones de flags
  5. Reporta los que produjeron al menos un bloque SRT

Sequential (un multiplex a la vez) para mantenerlo simple — total ~5 min.

Run: ./venv/bin/python scripts/scan-cc.py
     o:  bash -c "cd /home/sintonizador && .venv/bin/python scripts/scan-cc.py"

Requiere: el server uvicorn NO debe estar corriendo (los frontends son
exclusivos). El script usa Frontend + Demux directamente.
"""

from __future__ import annotations

import asyncio
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path

# Permitir correr desde scripts/ sin instalar el paquete
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from sintonizador.channels import Channel, parse_file
from sintonizador.dvb import constants as C
from sintonizador.dvb.frontend import Frontend
from sintonizador.extract.capture import capture_ts_seconds


CAPTURE_SECONDS = 15
LOCK_WAIT_SECONDS = 3.0
ADAPTER = 0
CHANNELS_FILE = "/home/sintonizador/channels.conf"
TS_TEMP = Path("/tmp/cc-scan.ts")


CC_COMBOS: dict[str, list[str]] = {
    "default":     [],
    "CC1":         ["-1"],
    "CC2":         ["-2"],
    "CC3":         ["-3"],
    "CC4":         ["-4"],
    "12":          ["-12"],
    "svc-all":     ["-svc", "all"],
    "autoprogram": ["-autoprogram"],
}


@dataclass
class MuxResult:
    freq_hz: int
    channels: list[Channel]
    locked: bool
    bytes_captured: int
    cc_hits: dict[str, dict]  # label -> {blocks, preview}


async def scan_one(adapter: int, channel: Channel) -> MuxResult:
    res = MuxResult(
        freq_hz=channel.frequency_hz,
        channels=[channel],
        locked=False,
        bytes_captured=0,
        cc_hits={},
    )
    with Frontend(adapter=adapter) as fe:
        fe.tune(channel.frequency_hz, channel.delivery_system, channel.modulation)
        # Esperar lock
        t0 = time.monotonic()
        while time.monotonic() - t0 < LOCK_WAIT_SECONDS:
            await asyncio.sleep(0.15)
            if fe.read_status().has_lock:
                res.locked = True
                break
        if not res.locked:
            return res
        # Capturar
        try:
            res.bytes_captured = await capture_ts_seconds(
                adapter=adapter, seconds=CAPTURE_SECONDS, out_path=TS_TEMP
            )
        except Exception as e:
            print(f"  capture error: {e}", file=sys.stderr)
            return res
        # Liberar tune
        try:
            fe.clear()
        except Exception:
            pass

    if res.bytes_captured < 188 * 100:
        return res

    # Probar combos de ccextractor
    for label, args in CC_COMBOS.items():
        try:
            r = subprocess.run(
                [
                    "ccextractor",
                    *args,
                    "-out=srt",
                    "-stdout",
                    "--nofontcolor",
                    "--norollup",
                    "-quiet",
                    str(TS_TEMP),
                ],
                capture_output=True,
                timeout=60,
            )
            srt = r.stdout.decode("utf-8", errors="replace")
            blocks = srt.count("-->")
            if blocks > 0:
                preview = srt.strip().split("\n", 6)
                res.cc_hits[label] = {
                    "blocks": blocks,
                    "preview": " ⏎ ".join(p for p in preview if p.strip())[:200],
                }
        except subprocess.TimeoutExpired:
            pass
    return res


async def main() -> None:
    chs = parse_file(CHANNELS_FILE)
    # Agrupar por multiplex
    muxes: dict[int, list[Channel]] = {}
    for c in chs:
        muxes.setdefault(c.frequency_hz, []).append(c)
    sorted_muxes = sorted(muxes.items())
    total = len(sorted_muxes)

    print(f"Scaneando {total} multiplexes · capture={CAPTURE_SECONDS}s · adapter={ADAPTER}")
    print()
    results: list[MuxResult] = []
    for i, (freq, mux_chs) in enumerate(sorted_muxes, 1):
        vchs = ", ".join(c.vchannel or "?" for c in mux_chs)
        print(f"[{i:>2}/{total}] {freq/1e6:>7.3f} MHz · {vchs}…", flush=True)
        res = await scan_one(adapter=ADAPTER, channel=mux_chs[0])
        res.channels = mux_chs
        results.append(res)
        if not res.locked:
            print(f"        ✗ no lock")
        elif not res.cc_hits:
            print(f"        ✗ sin CC ({res.bytes_captured/1e6:.1f} MB capturados)")
        else:
            print(f"        ✓ CC en: {', '.join(res.cc_hits.keys())}")
            for label, data in res.cc_hits.items():
                print(f"          [{label}] {data['blocks']} bloques · preview: {data['preview']}")
        # Pausa breve entre muxes para que el frontend se relaje
        await asyncio.sleep(0.5)

    # Resumen tabular final
    print()
    print("=" * 72)
    print("RESUMEN")
    print("=" * 72)
    print(f"{'Mux (MHz)':>10}  {'CC?':<3}  {'flags exitosos':<28}  {'subcanales'}")
    print("-" * 72)
    n_with_cc = 0
    for r in results:
        vchs = ", ".join(c.vchannel or "?" for c in r.channels)
        mark = "✗"
        flags = ""
        if not r.locked:
            mark = "—"
            flags = "(no lock)"
        elif r.cc_hits:
            mark = "✓"
            flags = ", ".join(r.cc_hits.keys())
            n_with_cc += 1
        print(f"{r.freq_hz/1e6:>10.3f}  {mark:<3}  {flags:<28}  {vchs}")
    print()
    print(f"Total con CC decodificable: {n_with_cc} / {total} multiplexes")

    # Cleanup
    try:
        TS_TEMP.unlink()
    except OSError:
        pass


if __name__ == "__main__":
    asyncio.run(main())
