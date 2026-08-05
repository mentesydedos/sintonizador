"""`MuxReaderRegistry`: garantiza UN solo `MuxReader` (un tap) por adapter.

Punto único por el que pasan TODOS los que quieren leer el TS de un adapter:
el archiver 24×7, el monitoreo interactivo (CC live + HLS) y las capturas
puntuales de estado. Mantener esto centralizado es lo que hace cumplir el
invariante de un-tap-por-adapter.
"""

from __future__ import annotations

import asyncio
import logging

from sintonizador.mux.reader import MuxReader, TsConsumer

log = logging.getLogger(__name__)


class MuxReaderRegistry:
    def __init__(self) -> None:
        self._readers: dict[int, MuxReader] = {}
        self._lock = asyncio.Lock()

    def get(self, adapter: int) -> MuxReader | None:
        return self._readers.get(adapter)

    async def attach(self, adapter: int, consumer: TsConsumer) -> MuxReader:
        """Cuelga `consumer` del reader del adapter (creándolo si no existe)."""
        async with self._lock:
            reader = self._readers.get(adapter)
            if reader is None:
                reader = MuxReader(adapter=adapter)
                self._readers[adapter] = reader
        await reader.add_consumer(consumer)
        return reader

    async def detach(self, adapter: int, key: str) -> None:
        """Quita el consumidor `key` del adapter. GC del reader si queda vacío."""
        reader = self._readers.get(adapter)
        if reader is None:
            return
        await reader.remove_consumer(key)
        if not reader.consumer_keys:
            async with self._lock:
                # Re-chequear bajo lock: otro attach pudo agregar uno entremedio.
                if adapter in self._readers and not self._readers[adapter].consumer_keys:
                    del self._readers[adapter]
                    log.debug("registry: reader del adapter %d removido (vacío)", adapter)

    def snapshot(self) -> dict[int, dict]:
        """Estado para el endpoint /mux de debug: consumidores y bytes por adapter."""
        return {
            adapter: {
                "consumers": reader.consumer_keys,
                "bytes_read": reader.bytes_read,
                "running": reader.is_running,
            }
            for adapter, reader in sorted(self._readers.items())
        }

    async def stop_all(self) -> None:
        async with self._lock:
            readers = list(self._readers.values())
            self._readers.clear()
        for reader in readers:
            try:
                await reader.stop()
            except Exception:
                log.exception("registry: error parando reader adapter %d", reader.adapter)
