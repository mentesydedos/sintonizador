"""`Archiver`: orquesta N `ArchivePipeline` y la asignación de adapters.

Al arrancar:
  1. Tunea cada adapter (vía `MonitorPoller.tune`) al multiplex asignado.
  2. Marca esos adapters como reservados (la API de tune los rechaza con 409).
  3. Espera un par de segundos para que los frontends lockean.
  4. Arranca una `ArchivePipeline` por subcanal en paralelo.

Al parar:
  1. Stop de todas las pipelines (parallel).
  2. Libera la reserva de los adapters.
  3. (No hacemos clear() del frontend — quizás el user quiera seguir
     monitoreando esos multiplexes después de parar el archive.)
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections import defaultdict
from pathlib import Path
from typing import TYPE_CHECKING, Any

from sintonizador.archiver.adapter_reader import AdapterReader
from sintonizador.archiver.config import (
    DEFAULT_MULTIPLEX_FREQS_HZ,
    ArchiveTarget,
    build_targets,
)
from sintonizador.archiver.pipeline import ArchivePipeline

if TYPE_CHECKING:
    from sintonizador.channels import Channel
    from sintonizador.monitor import MonitorPoller

log = logging.getLogger(__name__)


class Archiver:
    def __init__(
        self,
        poller: "MonitorPoller",
        channels: list["Channel"],
        archive_root: Path,
        multiplex_freqs_hz: list[int] | None = None,
        rotation_minutes: int = 30,
    ) -> None:
        self.poller = poller
        self.archive_root = Path(archive_root)
        self.rotation_minutes = rotation_minutes
        self.targets: list[ArchiveTarget] = build_targets(
            channels=channels,
            multiplex_freqs_hz=multiplex_freqs_hz or list(DEFAULT_MULTIPLEX_FREQS_HZ),
        )
        self.pipelines: list[ArchivePipeline] = []
        # readers[adapter] = AdapterReader que tiene las pipelines de ese adapter
        self.readers: dict[int, AdapterReader] = {}
        self._running = False
        self._started_at: float | None = None

    @property
    def is_running(self) -> bool:
        return self._running

    @property
    def reserved_adapters(self) -> set[int]:
        return {t.adapter for t in self.targets} if self._running else set()

    async def start(self) -> None:
        if self._running:
            return
        if not self.targets:
            log.warning("archiver: lista de targets vacía — nothing to do")
            return
        self.archive_root.mkdir(parents=True, exist_ok=True)

        # Tunear cada adapter al multiplex correspondiente (uno por adapter)
        seen: set[int] = set()
        for t in self.targets:
            if t.adapter in seen:
                continue
            seen.add(t.adapter)
            try:
                self.poller.tune(t.adapter, t.frequency_hz, t.delivery_system, t.modulation)
                log.info("archiver: tuned adapter %d → %.3f MHz (%d subcanales)",
                         t.adapter, t.frequency_hz / 1e6,
                         sum(1 for x in self.targets if x.adapter == t.adapter))
            except Exception:
                log.exception("archiver: tune adapter %d falló", t.adapter)

        # Reservar adapters en el poller (tune API los rechaza con 409)
        for adapter in seen:
            self.poller.reserve(adapter)

        # Dar 2s para que los frontends lockean antes de meter ccextractor a leer
        await asyncio.sleep(2.0)

        # Agrupar targets por adapter → 1 AdapterReader por adapter,
        # con N pipelines (una por subcanal) cada uno
        by_adapter: dict[int, list[ArchiveTarget]] = defaultdict(list)
        for t in self.targets:
            by_adapter[t.adapter].append(t)

        self.pipelines = []
        self.readers = {}
        for adapter, targets in by_adapter.items():
            pipelines = [
                ArchivePipeline(
                    target=t,
                    archive_root=self.archive_root,
                    rotation_minutes=self.rotation_minutes,
                )
                for t in targets
            ]
            self.pipelines.extend(pipelines)
            self.readers[adapter] = AdapterReader(adapter=adapter, pipelines=pipelines)

        # Arrancar todos los readers (cada uno arranca sus pipelines internamente)
        await asyncio.gather(*(r.start() for r in self.readers.values()))
        self._running = True
        self._started_at = time.time()
        log.info("archiver: %d adapters · %d pipelines · archive_root=%s",
                 len(self.readers), len(self.pipelines), self.archive_root)

    async def stop(self) -> None:
        if not self._running:
            return
        log.info("archiver: parando %d readers…", len(self.readers))
        # Liberar reserva primero — la UI puede usar los tuners enseguida.
        for adapter in {t.adapter for t in self.targets}:
            self.poller.release(adapter)
        # Stop readers en paralelo (cada uno para sus pipelines)
        await asyncio.gather(*(r.stop() for r in self.readers.values()), return_exceptions=True)
        self.readers = {}
        self.pipelines = []
        self._running = False
        log.info("archiver: stopped")

    def status(self) -> dict[str, Any]:
        return {
            "running": self._running,
            "started_at": self._started_at,
            "uptime_seconds": (time.time() - self._started_at) if self._started_at else 0,
            "archive_root": str(self.archive_root),
            "rotation_minutes": self.rotation_minutes,
            "total_targets": len(self.targets),
            "reserved_adapters": sorted(self.reserved_adapters),
            # Bytes leídos por adapter — útil para detectar si un adapter
            # pierde su feed RF o el demux fd colapsa
            "adapter_bytes_read": {
                str(a): r.bytes_read for a, r in self.readers.items()
            },
            "pipelines": [
                {
                    "slug": p.target.slug,
                    "adapter": p.target.adapter,
                    "frequency_mhz": round(p.target.frequency_hz / 1e6, 3),
                    "program_id": p.target.program_id,
                    "channel_name": p.target.channel_name,
                    "vchannel": p.target.vchannel,
                    "active": p.is_active,
                    "blocks_written": p.stats.blocks_written,
                    "bytes_processed": p.stats.bytes_to_ccextractor,
                    "ccextractor_restarts": p.stats.ccextractor_restarts,
                    "last_event_age_s": (
                        round(time.time() - p.stats.last_event_time, 1)
                        if p.stats.last_event_time else None
                    ),
                    "current_period_start": (
                        p.stats.current_period_start.isoformat()
                        if p.stats.current_period_start else None
                    ),
                    "current_srt_path": p.stats.current_srt_path,
                    "current_txt_path": p.stats.current_txt_path,
                    "last_error": p.stats.last_error,
                }
                for p in self.pipelines
            ],
        }
