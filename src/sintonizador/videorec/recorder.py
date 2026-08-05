"""`VideoRecorder`: orquesta N `VideoRecordPipeline`, una por subcanal.

A diferencia de `Archiver` (CC), este orquestador es deliberadamente más
delgado: NO tunea ni reserva adapters. Depende de que el `Archiver` de
Closed Captions ya esté corriendo y tuneado sobre los multiplexes de interés
— mismo patrón que ya usa `monitor/transcode.py` para coexistir con el
archiver ("archiver y monitoreo comparten el mismo reader cuando coinciden en
mux", ver README). Reusa la MISMA lista de `ArchiveTarget` que construyó el
archiver, así adapter↔frecuencia siempre coinciden 1:1 entre CC y video.

Al arrancar:
  1. Verifica que `archiver.is_running` — si no, no hace nada (warning).
  2. Asigna encoder por target: los primeros `nvenc_limit` → NVENC, el resto
     → libx264 (contador global, no por adapter). El techo real de NVENC de
     la tarjeta es desconocido hasta probarlo — `nvenc_limit` es config, no
     una constante.
  3. Crea una `VideoRecordPipeline` por target y la cuelga del
     `MuxReaderRegistry` (mismo adapter, mismo mux, ya tuneado por Archiver).

Al parar: solo `registry.detach(...)` de cada pipeline — NUNCA
`poller.reserve`/`release`, eso sigue siendo responsabilidad exclusiva de
`Archiver`.
"""

from __future__ import annotations

import logging
import time
from pathlib import Path
from typing import TYPE_CHECKING, Any

from sintonizador.videorec.pipeline import VideoRecordPipeline

if TYPE_CHECKING:
    from sintonizador.archiver import Archiver
    from sintonizador.mux import MuxReaderRegistry

log = logging.getLogger(__name__)


class VideoRecorder:
    def __init__(
        self,
        archiver: "Archiver",
        registry: "MuxReaderRegistry",
        output_root: Path,
        encoder: str = "nvenc",
        nvenc_limit: int = 4,
        bitrate: str = "5M",
        maxrate: str = "6M",
        bufsize: str = "10M",
        rotation_minutes: int = 30,
    ) -> None:
        self.archiver = archiver
        self.registry = registry
        self.output_root = Path(output_root)
        self.encoder = encoder
        self.nvenc_limit = nvenc_limit
        self.bitrate = bitrate
        self.maxrate = maxrate
        self.bufsize = bufsize
        self.rotation_minutes = rotation_minutes
        self.pipelines: list[VideoRecordPipeline] = []
        self._running = False
        self._started_at: float | None = None

    @property
    def is_running(self) -> bool:
        return self._running

    async def start(self) -> None:
        if self._running:
            return
        if not self.archiver.is_running:
            log.warning("videorec: el archiver de CC no está corriendo — "
                        "nada que grabar (los adapters no están tuneados/reservados)")
            return
        targets = self.archiver.targets
        if not targets:
            log.warning("videorec: lista de targets vacía — nothing to do")
            return

        self.output_root.mkdir(parents=True, exist_ok=True)
        self.pipelines = []
        for i, t in enumerate(targets, start=1):
            # Encoder por target: primeros nvenc_limit -> nvenc, resto -> software.
            # Si el usuario forzó SINTONIZADOR_VIDEOREC_ENCODER=software, todos van
            # por CPU (sin importar el índice).
            if self.encoder == "nvenc" and i <= self.nvenc_limit:
                enc = "nvenc"
            else:
                enc = "software"
            pipeline = VideoRecordPipeline(
                target=t,
                channel_id=i,
                output_root=self.output_root,
                encoder=enc,
                bitrate=self.bitrate,
                maxrate=self.maxrate,
                bufsize=self.bufsize,
                rotation_minutes=self.rotation_minutes,
            )
            self.pipelines.append(pipeline)
            await self.registry.attach(t.adapter, pipeline)

        self._running = True
        self._started_at = time.time()
        n_nvenc = sum(1 for p in self.pipelines if p.encoder == "nvenc")
        log.info("videorec: %d pipelines (%d nvenc / %d software) · output_root=%s",
                 len(self.pipelines), n_nvenc, len(self.pipelines) - n_nvenc, self.output_root)

    async def stop(self) -> None:
        if not self._running:
            return
        log.info("videorec: parando %d pipelines…", len(self.pipelines))
        for p in self.pipelines:
            try:
                await self.registry.detach(p.target.adapter, p.key)
            except Exception:
                log.exception("videorec: detach %s falló", p.key)
        self.pipelines = []
        self._running = False
        log.info("videorec: stopped")

    def status(self) -> dict[str, Any]:
        return {
            "running": self._running,
            "started_at": self._started_at,
            "uptime_seconds": (time.time() - self._started_at) if self._started_at else 0,
            "output_root": str(self.output_root),
            "rotation_minutes": self.rotation_minutes,
            "nvenc_limit": self.nvenc_limit,
            "pipelines": [
                {
                    "slug": p.target.slug,
                    "channel_id": p.channel_id,
                    "channel_name": p.channel_name,
                    "adapter": p.target.adapter,
                    "frequency_mhz": round(p.target.frequency_hz / 1e6, 3),
                    "encoder": p.stats.encoder_used,
                    "active": p.is_active,
                    "bytes_processed": p.stats.bytes_to_ffmpeg,
                    "ffmpeg_restarts": p.stats.ffmpeg_restarts,
                    "current_segment_glob": p.stats.current_segment_path,
                    "last_error": p.stats.last_error,
                    "out_dir": str(p.out_dir),
                }
                for p in self.pipelines
            ],
        }
