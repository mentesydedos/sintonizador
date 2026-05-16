"""Captura no-bloqueante de N segundos de TS de un adapter ya sintonizado.

Usa `DMX_OUT_TSDEMUX_TAP` — el TS sale por el fd del propio demux, sin
interferir con un consumer de `/dev/dvb/adapter{N}/dvr0` que esté activo.
"""

from __future__ import annotations

import asyncio
import logging
import os
import time
from pathlib import Path

from sintonizador.dvb.demux import Demux

log = logging.getLogger(__name__)

# Mismo chunk que el endpoint /stream (188 × 200 = 37.6 KB)
_CHUNK = 188 * 200


async def capture_ts_seconds(adapter: int, seconds: float, out_path: Path) -> int:
    """Captura `seconds` segundos de TS del adapter a `out_path`.

    El adapter debe estar previamente sintonizado (frontend con freq != 0).
    Devuelve los bytes escritos. Tira si falla la apertura/configuración
    del demux.
    """
    loop = asyncio.get_running_loop()
    written = 0
    dmx = Demux(adapter=adapter)
    try:
        dmx.open()
        dmx.set_filter_all_pids_tsdemux_tap(buffer_kb=512)
        # Poner el fd en non-blocking para que reads cortos no nos cuelguen
        os.set_blocking(dmx.fd, False)

        deadline = time.monotonic() + seconds
        with open(out_path, "wb") as out:
            while time.monotonic() < deadline:
                try:
                    chunk = await loop.run_in_executor(None, _read_or_empty, dmx.fd)
                except OSError as e:
                    log.warning("capture(adapter=%d): read error: %s", adapter, e)
                    break
                if chunk:
                    out.write(chunk)
                    written += len(chunk)
                else:
                    # Buffer vacío — pequeña espera para no spinear el CPU
                    await asyncio.sleep(0.02)
    finally:
        dmx.close()
    log.info("capture(adapter=%d): %d bytes en %.2fs", adapter, written, seconds)
    return written


def _read_or_empty(fd: int) -> bytes:
    """os.read tolerante a EAGAIN (devuelve b'' si no hay datos listos)."""
    try:
        return os.read(fd, _CHUNK)
    except BlockingIOError:
        return b""
