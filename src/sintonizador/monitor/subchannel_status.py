"""Estado por subcanal: probe de codec/idioma/CC sin abrir un segundo tap.

`CaptureConsumer` es un `TsConsumer` que graba ~N segundos del mux a un archivo
temporal colgándose del `MuxReader` compartido del adapter — así NO duplica el
tap (constraint del kernel). Después corremos ffprobe (filtrado al `service_id`
del subcanal) y ccextractor (`-pn`) sobre la captura.
"""

from __future__ import annotations

import asyncio
import logging
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from sintonizador.extract.tools import run_ccextractor, run_ffprobe
from sintonizador.mux import MuxReaderRegistry

log = logging.getLogger(__name__)


@dataclass
class SubchannelStatus:
    slug: str
    channel_id: int
    adapter: int | None  # qué tuner lleva su mux ahora (None si no sintonizado)
    frequency_hz: int
    service_id: int | None
    on_air: bool  # has_lock en su mux
    video_codec: str | None = None
    width: int | None = None
    height: int | None = None
    audio_codec: str | None = None
    audio_language: str | None = None
    cc_present: bool = False
    bytes_captured: int = 0
    last_probe_at: float | None = None
    error: str | None = None


class CaptureConsumer:
    """Graba `seconds` de TS a un archivo, colgándose del MuxReader del adapter.

    Resuelve un future con los bytes escritos cuando se cumple la ventana o se
    para. NO abre demux propio (lo provee el MuxReader compartido).
    """

    def __init__(self, key: str, out_path: Path, seconds: float) -> None:
        self._key = key
        self.out_path = out_path
        self.seconds = seconds
        self._fh = None
        self._deadline: float | None = None
        self._written = 0
        self._done: asyncio.Future[int] = asyncio.get_event_loop().create_future()

    @property
    def key(self) -> str:
        return self._key

    async def start(self) -> None:
        self._fh = open(self.out_path, "wb")
        self._deadline = None  # se fija con el primer chunk (captura limpia)

    def feed_ts(self, data: bytes) -> None:
        if self._fh is None:
            return
        now = time.monotonic()
        if self._deadline is None:
            self._deadline = now + self.seconds
        if now >= self._deadline:
            self._finish()
            return
        try:
            self._fh.write(data)
            self._written += len(data)
        except OSError:
            self._finish()

    async def stop(self) -> None:
        self._finish()

    def _finish(self) -> None:
        if self._fh is not None:
            try:
                self._fh.close()
            except OSError:
                pass
            self._fh = None
        if not self._done.done():
            self._done.set_result(self._written)

    async def wait(self) -> int:
        return await self._done


async def capture_via_reader(
    registry: MuxReaderRegistry, adapter: int, seconds: float
) -> tuple[Path, int]:
    """Graba `seconds` del mux del adapter usando el MuxReader compartido.

    Devuelve (path_temporal, bytes). El caller debe borrar el archivo.
    """
    with tempfile.NamedTemporaryFile(
        suffix=".ts", prefix=f"sint-probe-a{adapter}-", delete=False
    ) as tf:
        out_path = Path(tf.name)
    key = f"capture:{adapter}:{int(time.monotonic() * 1000)}"
    consumer = CaptureConsumer(key=key, out_path=out_path, seconds=seconds)
    await registry.attach(adapter, consumer)
    try:
        await asyncio.wait_for(consumer.wait(), timeout=seconds + 4.0)
    except asyncio.TimeoutError:
        log.warning("capture_via_reader(adapter=%d): timeout", adapter)
    finally:
        await registry.detach(adapter, key)
    return out_path, consumer._written


def _pick_program(ffprobe: dict, service_id: int | None) -> dict | None:
    progs = ffprobe.get("programs", []) if isinstance(ffprobe, dict) else []
    if service_id is not None:
        for p in progs:
            if p.get("program_id") == service_id:
                return p
    # Fallback: si no matchea, devolver el primero
    return progs[0] if progs else None


async def probe_subchannel(
    registry: MuxReaderRegistry,
    adapter: int,
    service_id: int | None,
    seconds: float = 3.0,
) -> dict[str, Any]:
    """Captura y extrae codec/resolución/idioma/CC del subcanal `service_id`.

    Devuelve dict con keys: video_codec, width, height, audio_codec,
    audio_language, cc_present, bytes_captured, error.
    """
    out_path, written = await capture_via_reader(registry, adapter, seconds)
    result: dict[str, Any] = {"bytes_captured": written}
    try:
        if written < 188 * 10:
            result["error"] = f"captura demasiado corta ({written} bytes)"
            return result
        ffprobe, cc = await asyncio.gather(
            run_ffprobe(out_path),
            run_ccextractor(out_path, program_id=service_id),
        )
        prog = _pick_program(ffprobe, service_id)
        if prog:
            for s in prog.get("streams", []):
                if s.get("codec_type") == "video" and "video_codec" not in result:
                    result["video_codec"] = s.get("codec_name")
                    result["width"] = s.get("width")
                    result["height"] = s.get("height")
                elif s.get("codec_type") == "audio" and "audio_codec" not in result:
                    result["audio_codec"] = s.get("codec_name")
                    tags = s.get("tags") or {}
                    result["audio_language"] = tags.get("language") or tags.get("lang")
        result["cc_present"] = bool(cc.get("text", "").strip())
        return result
    finally:
        try:
            out_path.unlink()
        except OSError:
            pass
