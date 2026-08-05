"""Video preview por subcanal: transcode del programa → H.264/AAC → HLS.

`HlsConsumer` es un `TsConsumer` (se cuelga del MuxReader compartido → sin
segundo tap) que alimenta UN ffmpeg por stdin. ffmpeg selecciona el programa
del mux (`-map 0:p:{service_id}`), reescala y transcodifica a H.264 + AAC y
empaqueta HLS en un dir temporal que sirve FastAPI.

Encoder: por defecto `libx264 -preset ultrafast` (software). El iGPU de la Z2
no transcodifica de forma fiable hoy (QSV sin runtime oneVPL; encoder VAAPI con
bug del driver iHD) — ver memoria project_transcode_video_web. Flag de env
`SINTONIZADOR_HLS_ENCODER` (software|vaapi|qsv) para reactivar HW.

`TranscodeManager` ref-cuenta por slug, impone un tope global de transcodes
simultáneos (el iGPU/CPU no da para 16) y mata por inactividad (si el browser
deja de pedir el playlist).
"""

from __future__ import annotations

import asyncio
import logging
import os
import shutil
import time
from pathlib import Path

from sintonizador.channels import Channel
from sintonizador.archiver.config import make_slug
from sintonizador.mux import MuxReaderRegistry

log = logging.getLogger(__name__)

_RESTART_DELAY_S = 1.0


def _build_ffmpeg_cmd(
    encoder: str, service_id: int, out_dir: Path, width: int, height: int, vbitrate: str
) -> list[str]:
    """Arma el comando ffmpeg según el encoder elegido. Lee el mux por stdin."""
    base = [
        "ffmpeg", "-hide_banner", "-loglevel", "error",
        "-fflags", "+genpts", "-flags", "low_delay",
    ]
    if encoder == "qsv":
        base += ["-hwaccel", "qsv", "-hwaccel_output_format", "qsv"]
    elif encoder == "vaapi":
        base += [
            "-hwaccel", "vaapi",
            "-hwaccel_device", "/dev/dri/renderD128",
        ]
    inp = ["-i", "pipe:0", "-map", f"0:p:{service_id}", "-sn"]

    if encoder == "qsv":
        venc = ["-c:v", "h264_qsv", "-vf", f"scale_qsv=w={width}:h={height}", "-b:v", vbitrate]
    elif encoder == "vaapi":
        venc = [
            "-vf", f"scale=w={width}:h={height},format=nv12,hwupload",
            "-c:v", "h264_vaapi", "-b:v", vbitrate,
        ]
    else:  # software (default)
        venc = [
            "-c:v", "libx264", "-preset", "ultrafast", "-tune", "zerolatency",
            "-vf", f"scale={width}:{height}", "-b:v", vbitrate, "-g", "48",
        ]
    aenc = ["-c:a", "aac", "-b:a", "96k", "-ac", "2"]
    out = [
        "-f", "hls", "-hls_time", "2", "-hls_list_size", "6",
        "-hls_flags", "delete_segments+append_list+omit_endlist",
        "-hls_segment_filename", str(out_dir / "seg_%05d.ts"),
        str(out_dir / "playlist.m3u8"),
    ]
    return base + inp + venc + aenc + out


class HlsConsumer:
    """`TsConsumer` que transcodifica un programa del mux a HLS."""

    def __init__(
        self, slug: str, service_id: int, out_dir: Path,
        encoder: str = "software", width: int = 640, height: int = 360, vbitrate: str = "1M",
    ) -> None:
        self.slug = slug
        self.service_id = service_id
        self.out_dir = out_dir
        self.encoder = encoder
        self.width = width
        self.height = height
        self.vbitrate = vbitrate
        self._feed_queue: asyncio.Queue[bytes] = asyncio.Queue(maxsize=80)
        self._task: asyncio.Task | None = None
        self._pump_task: asyncio.Task | None = None
        self._stopping = asyncio.Event()
        self._proc: asyncio.subprocess.Process | None = None
        self.restarts = 0
        self.last_error: str | None = None

    @property
    def key(self) -> str:
        return f"hls:{self.slug}"

    async def start(self) -> None:
        if self._task is not None and not self._task.done():
            return
        self.out_dir.mkdir(parents=True, exist_ok=True)
        self._stopping = asyncio.Event()
        self._feed_queue = asyncio.Queue(maxsize=80)
        self._task = asyncio.create_task(self._run(), name=f"hls-{self.slug}")
        self._pump_task = asyncio.create_task(self._pump_loop(), name=f"hls-pump-{self.slug}")

    async def stop(self) -> None:
        self._stopping.set()
        for t in (self._task, self._pump_task):
            if t is not None:
                try:
                    await asyncio.wait_for(t, timeout=5.0)
                except asyncio.TimeoutError:
                    t.cancel()
        self._task = None
        self._pump_task = None
        # Limpiar segmentos/playlist
        try:
            shutil.rmtree(self.out_dir, ignore_errors=True)
        except OSError:
            pass

    def feed_ts(self, data: bytes) -> None:
        if self._stopping.is_set():
            return
        try:
            self._feed_queue.put_nowait(data)
        except asyncio.QueueFull:
            try:
                self._feed_queue.get_nowait()  # drop oldest
                self._feed_queue.put_nowait(data)
            except (asyncio.QueueEmpty, asyncio.QueueFull):
                pass

    async def _pump_loop(self) -> None:
        while not self._stopping.is_set():
            try:
                data = await asyncio.wait_for(self._feed_queue.get(), timeout=1.0)
            except asyncio.TimeoutError:
                continue
            proc = self._proc
            if proc is None or proc.stdin is None or proc.stdin.is_closing():
                continue
            try:
                proc.stdin.write(data)
                await proc.stdin.drain()
            except (BrokenPipeError, ConnectionResetError):
                pass

    async def _run(self) -> None:
        log.info("hls %s: starting (encoder=%s program=%d %dx%d)",
                 self.slug, self.encoder, self.service_id, self.width, self.height)
        # Limitamos restarts: si ffmpeg muere repetido (programa inválido, etc.)
        # no spamear procesos para siempre.
        while not self._stopping.is_set() and self.restarts < 5:
            try:
                await self._run_one_lifetime()
            except asyncio.CancelledError:
                raise
            except Exception as e:
                self.last_error = str(e)
                log.exception("hls %s: error en lifetime", self.slug)
            if self._stopping.is_set():
                break
            self.restarts += 1
            try:
                await asyncio.wait_for(self._stopping.wait(), timeout=_RESTART_DELAY_S)
            except asyncio.TimeoutError:
                pass
        log.info("hls %s: stopped (restarts=%d)", self.slug, self.restarts)

    async def _run_one_lifetime(self) -> None:
        cmd = _build_ffmpeg_cmd(
            self.encoder, self.service_id, self.out_dir, self.width, self.height, self.vbitrate
        )
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.PIPE,
        )
        self._proc = proc
        log.info("hls %s: ffmpeg pid=%d", self.slug, proc.pid)
        try:
            await self._stderr_drain(proc)
        finally:
            self._proc = None
            if proc.returncode is None:
                try:
                    proc.terminate()
                    await asyncio.wait_for(proc.wait(), timeout=2.0)
                except (asyncio.TimeoutError, ProcessLookupError):
                    try: proc.kill()
                    except ProcessLookupError: pass

    async def _stderr_drain(self, proc: asyncio.subprocess.Process) -> None:
        assert proc.stderr is not None
        while not self._stopping.is_set():
            line = await proc.stderr.readline()
            if not line:
                break
            msg = line.decode(errors="replace").rstrip()
            if msg:
                self.last_error = msg
                log.warning("ffmpeg[%s]: %s", self.slug, msg)


class TranscodeManager:
    """Ref-cuenta `HlsConsumer` por slug; tope global + reaper por inactividad."""

    IDLE_TIMEOUT_S = 25.0

    def __init__(
        self,
        registry: MuxReaderRegistry,
        hls_root: Path,
        max_concurrent: int = 5,
        encoder: str = "software",
        width: int = 640,
        height: int = 360,
        vbitrate: str = "1M",
    ) -> None:
        self.registry = registry
        self.hls_root = Path(hls_root)
        self.max_concurrent = max_concurrent
        self.encoder = encoder
        self.width = width
        self.height = height
        self.vbitrate = vbitrate
        self._consumers: dict[str, HlsConsumer] = {}
        self._adapter: dict[str, int] = {}
        self._last_access: dict[str, float] = {}
        self._lock = asyncio.Lock()
        self._reaper_task: asyncio.Task | None = None

    async def start(self) -> None:
        self.hls_root.mkdir(parents=True, exist_ok=True)
        if self._reaper_task is None:
            self._reaper_task = asyncio.create_task(self._reaper(), name="hls-reaper")

    @property
    def active_slugs(self) -> list[str]:
        return sorted(self._consumers)

    def count(self) -> int:
        return len(self._consumers)

    def touch(self, slug: str) -> bool:
        """Marca acceso reciente al playlist. True si el slug está activo."""
        if slug in self._consumers:
            self._last_access[slug] = time.monotonic()
            return True
        return False

    async def start_transcode(self, channel: Channel, adapter: int) -> dict:
        """Arranca (o reusa) el transcode del subcanal. Devuelve {slug, playlist}."""
        slug = make_slug(channel.vchannel, channel.name)
        if channel.service_id is None:
            raise ValueError("canal sin service_id — no se puede seleccionar el programa")
        async with self._lock:
            if slug in self._consumers:
                self._last_access[slug] = time.monotonic()
                return {"slug": slug, "playlist": f"/hls/{slug}/playlist.m3u8", "reused": True}
            if len(self._consumers) >= self.max_concurrent:
                raise RuntimeError(
                    f"tope de {self.max_concurrent} previews de video alcanzado — "
                    f"cerrá alguno antes de abrir otro"
                )
            consumer = HlsConsumer(
                slug=slug, service_id=channel.service_id,
                out_dir=self.hls_root / slug,
                encoder=self.encoder, width=self.width, height=self.height, vbitrate=self.vbitrate,
            )
            await self.registry.attach(adapter, consumer)
            self._consumers[slug] = consumer
            self._adapter[slug] = adapter
            self._last_access[slug] = time.monotonic()
            log.info("transcode %s: arrancado (adapter %d, %d/%d activos)",
                     slug, adapter, len(self._consumers), self.max_concurrent)
            return {"slug": slug, "playlist": f"/hls/{slug}/playlist.m3u8", "reused": False}

    async def stop_transcode(self, slug: str) -> bool:
        async with self._lock:
            consumer = self._consumers.pop(slug, None)
            adapter = self._adapter.pop(slug, None)
            self._last_access.pop(slug, None)
        if consumer is None:
            return False
        if adapter is not None:
            await self.registry.detach(adapter, consumer.key)
        log.info("transcode %s: detenido", slug)
        return True

    async def _reaper(self) -> None:
        """Mata transcodes cuyo playlist no se pide hace IDLE_TIMEOUT_S."""
        try:
            while True:
                await asyncio.sleep(5.0)
                now = time.monotonic()
                stale = [
                    slug for slug, ts in list(self._last_access.items())
                    if now - ts > self.IDLE_TIMEOUT_S
                ]
                for slug in stale:
                    log.info("transcode %s: reaped por inactividad", slug)
                    await self.stop_transcode(slug)
        except asyncio.CancelledError:
            raise

    async def teardown(self) -> None:
        if self._reaper_task is not None:
            self._reaper_task.cancel()
            try:
                await self._reaper_task
            except asyncio.CancelledError:
                pass
            self._reaper_task = None
        for slug in list(self._consumers):
            await self.stop_transcode(slug)
        # Limpiar root
        try:
            shutil.rmtree(self.hls_root, ignore_errors=True)
        except OSError:
            pass
