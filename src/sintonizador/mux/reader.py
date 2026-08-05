"""`MuxReader`: dueño del ÚNICO tap (demux fd) de un adapter, con fan-out
en user-space a N consumidores tipados.

Generaliza el patrón probado de `archiver/adapter_reader.py`. La razón de existir
es el constraint del kernel (verificado 2026-05-14): abrir más de un
`TSDEMUX_TAP` por adapter duplica paquetes y satura el buffer → ccextractor
pierde bytes VBI. Por eso TODO consumidor (archive, CC live, HLS, captura
puntual) debe colgarse de un único reader por adapter:

    demux fd (único, TSDEMUX_TAP all-PIDs, buffer 4 MB)
       ↓ read en user-space
    fan-out síncrono → c.feed_ts(data) para cada consumidor

`feed_ts` debe ser NO-BLOQUEANTE (encolar con drop-oldest): un consumidor lento
no puede frenar el read-loop ni a sus hermanos del mismo adapter.
"""

from __future__ import annotations

import asyncio
import logging
import os
from typing import Protocol, runtime_checkable

from sintonizador.dvb.demux import Demux

log = logging.getLogger(__name__)


_CHUNK = 188 * 200
_DEMUX_BUFFER_KB = 4096


@runtime_checkable
class TsConsumer(Protocol):
    """Consumidor de bytes TS de un `MuxReader`.

    Implementaciones: `ArchivePipeline`, `LiveCCConsumer`, `HlsConsumer`,
    `CaptureConsumer`. Todas comparten el mismo contrato.
    """

    @property
    def key(self) -> str:
        """Identificador único dentro del adapter, p.ej. 'archive:2.1-XHGA'."""
        ...

    def feed_ts(self, data: bytes) -> None:
        """Recibe un chunk del TS. DEBE ser no-bloqueante (encolar/descartar)."""
        ...

    async def start(self) -> None: ...

    async def stop(self) -> None: ...


class MuxReader:
    """Un tap por adapter + fan-out a consumidores. Arranca el read-loop con el
    primer consumidor, lo apaga (y cierra el demux) con el último."""

    def __init__(self, adapter: int) -> None:
        self.adapter = adapter
        self._dmx = Demux(adapter=adapter)
        self._consumers: dict[str, TsConsumer] = {}
        self._task: asyncio.Task | None = None
        self._stopping = asyncio.Event()
        self._bytes_read = 0
        self._lock = asyncio.Lock()

    @property
    def bytes_read(self) -> int:
        return self._bytes_read

    @property
    def consumer_keys(self) -> list[str]:
        return sorted(self._consumers)

    @property
    def is_running(self) -> bool:
        return self._task is not None and not self._task.done()

    async def add_consumer(self, consumer: TsConsumer) -> None:
        """Arranca el consumidor y lo registra. Abre el tap si es el primero."""
        async with self._lock:
            if consumer.key in self._consumers:
                log.debug("MuxReader[%d]: consumer %s ya registrado", self.adapter, consumer.key)
                return
            await consumer.start()
            self._consumers[consumer.key] = consumer
            if self._task is None:
                try:
                    self._open_tap()
                except Exception:
                    # Rollback: si no podemos abrir el demux, parar el consumidor
                    self._consumers.pop(consumer.key, None)
                    await consumer.stop()
                    raise
                self._stopping = asyncio.Event()
                self._task = asyncio.create_task(
                    self._run(), name=f"muxreader-{self.adapter}"
                )
            log.info("MuxReader[%d]: +%s (consumers=%d)",
                     self.adapter, consumer.key, len(self._consumers))

    async def remove_consumer(self, key: str) -> None:
        """Para el consumidor `key`. Cierra el tap si era el último."""
        async with self._lock:
            consumer = self._consumers.pop(key, None)
            if consumer is None:
                return
            await consumer.stop()
            log.info("MuxReader[%d]: -%s (consumers=%d)",
                     self.adapter, key, len(self._consumers))
            if not self._consumers and self._task is not None:
                await self._stop_loop()

    async def stop(self) -> None:
        """Para todos los consumidores y cierra el tap."""
        async with self._lock:
            for consumer in list(self._consumers.values()):
                try:
                    await consumer.stop()
                except Exception:
                    log.exception("MuxReader[%d]: error parando %s", self.adapter, consumer.key)
            self._consumers.clear()
            if self._task is not None:
                await self._stop_loop()

    # --- internals (todos bajo self._lock) ---

    def _open_tap(self) -> None:
        self._dmx.open()
        self._dmx.set_filter_all_pids_tsdemux_tap(buffer_kb=_DEMUX_BUFFER_KB)
        os.set_blocking(self._dmx.fd, False)

    async def _stop_loop(self) -> None:
        self._stopping.set()
        if self._task is not None:
            try:
                await asyncio.wait_for(self._task, timeout=5.0)
            except asyncio.TimeoutError:
                log.warning("MuxReader[%d]: timeout al parar loop, cancel", self.adapter)
                self._task.cancel()
            self._task = None
        self._dmx.close()
        log.info("MuxReader[%d]: tap cerrado", self.adapter)

    async def _run(self) -> None:
        loop = asyncio.get_running_loop()
        while not self._stopping.is_set():
            try:
                data = await loop.run_in_executor(None, _read_or_empty, self._dmx.fd)
            except OSError as e:
                log.warning("MuxReader[%d]: read err: %s", self.adapter, e)
                break
            if not data:
                await asyncio.sleep(0.02)
                continue
            self._bytes_read += len(data)
            # Fan-out síncrono — feed_ts no bloquea. Snapshot de values() por si
            # un consumidor se quita concurrentemente (remove_consumer tiene lock,
            # pero el loop no; iterar sobre una copia evita RuntimeError).
            for consumer in list(self._consumers.values()):
                try:
                    consumer.feed_ts(data)
                except Exception:
                    log.exception("MuxReader[%d]: feed_ts(%s) explotó",
                                  self.adapter, consumer.key)


def _read_or_empty(fd: int) -> bytes:
    try:
        return os.read(fd, _CHUNK)
    except BlockingIOError:
        return b""
