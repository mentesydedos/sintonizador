"""`AdapterReader`: lee UN demux fd por adapter y distribuye los bytes a N
pipelines (una por subcanal del multiplex).

Antes teníamos un demux fd por subcanal → el kernel duplicaba paquetes para cada
fd. Con XHGA (TS al límite ATSC: video 1080p + 2 streams AC-3 5.1) esa
duplicación saturaba el buffer del kernel y se descartaban paquetes,
haciendo que ccextractor perdiera bytes VBI y emitiera 3× menos captions
que lo real. Verificado empíricamente 2026-05-14: captura limpia (1 fd)
14.7 cap/min vs paralela (3 fds) 5 cap/min.

Ahora el flujo es:
    demux fd (único, TSDEMUX_TAP all-PIDs, buffer 4 MB)
       ↓ read en user-space
    fan-out síncrono → stdin de N ccextractor (uno por subcanal del adapter)
"""

from __future__ import annotations

import asyncio
import logging
import os
from typing import TYPE_CHECKING

from sintonizador.dvb.demux import Demux

if TYPE_CHECKING:
    from sintonizador.archiver.pipeline import ArchivePipeline

log = logging.getLogger(__name__)


_CHUNK = 188 * 200
_DEMUX_BUFFER_KB = 4096


class AdapterReader:
    def __init__(self, adapter: int, pipelines: list["ArchivePipeline"]) -> None:
        self.adapter = adapter
        self.pipelines = pipelines
        self._dmx = Demux(adapter=adapter)
        self._task: asyncio.Task | None = None
        self._stopping = asyncio.Event()
        self._bytes_read = 0

    @property
    def bytes_read(self) -> int:
        return self._bytes_read

    async def start(self) -> None:
        if self._task is not None:
            return
        try:
            self._dmx.open()
            self._dmx.set_filter_all_pids_tsdemux_tap(buffer_kb=_DEMUX_BUFFER_KB)
            os.set_blocking(self._dmx.fd, False)
        except Exception:
            log.exception("AdapterReader[%d]: no se pudo abrir demux", self.adapter)
            raise
        # Arrancar todas las pipelines del adapter en paralelo
        await asyncio.gather(*(p.start() for p in self.pipelines))
        self._stopping = asyncio.Event()
        self._task = asyncio.create_task(self._run(), name=f"reader-adapter{self.adapter}")
        log.info("AdapterReader[%d]: started con %d pipelines",
                 self.adapter, len(self.pipelines))

    async def stop(self) -> None:
        self._stopping.set()
        if self._task is not None:
            try:
                await asyncio.wait_for(self._task, timeout=5.0)
            except asyncio.TimeoutError:
                log.warning("AdapterReader[%d]: timeout al parar, cancel", self.adapter)
                self._task.cancel()
            self._task = None
        # Parar pipelines (cierran ccextractor + archivos)
        await asyncio.gather(*(p.stop() for p in self.pipelines), return_exceptions=True)
        self._dmx.close()
        log.info("AdapterReader[%d]: stopped", self.adapter)

    async def _run(self) -> None:
        loop = asyncio.get_running_loop()
        while not self._stopping.is_set():
            try:
                data = await loop.run_in_executor(None, _read_or_empty, self._dmx.fd)
            except OSError as e:
                log.warning("AdapterReader[%d]: read err: %s", self.adapter, e)
                break
            if not data:
                await asyncio.sleep(0.02)
                continue
            self._bytes_read += len(data)
            # Fan-out sync — feed_ts no bloquea, así que ni await ni gather.
            # Una pipeline con ccextractor caído/lento no afecta a las demás.
            for p in self.pipelines:
                p.feed_ts(data)


def _read_or_empty(fd: int) -> bytes:
    try:
        return os.read(fd, _CHUNK)
    except BlockingIOError:
        return b""
