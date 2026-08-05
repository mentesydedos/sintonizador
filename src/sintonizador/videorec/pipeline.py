"""`VideoRecordPipeline`: un `TsConsumer` que graba un subcanal a disco 24x7.

NO maneja demux directamente — recibe bytes vía `feed_ts()` del `MuxReader`
compartido del adapter (mismo patrón que `ArchivePipeline` y `HlsConsumer` en
`monitor/transcode.py`). ffmpeg selecciona el programa del mux crudo
(`-map 0:p:{service_id}`), desentrelaza, codifica a H.264 (NVENC con fallback
a libx264 por CPU) + AAC, y rota el archivo de salida cada `rotation_minutes`
vía `-f segment` — la rotación la hace ffmpeg mismo, no hace falta manejarla
en Python como en `ArchivePipeline` (que rota a mano el .srt/.txt de
ccextractor).

Convención de carpeta/archivo IDÉNTICA a `video_recorder.py` de
transcriber-linux, para que `alerts/clips.py` (dashboard AlertaTV) lea estos
archivos sin ningún cambio: `canal_{id:02d}_{safe_name}/canal_{id:02d}_{safe_name}_%Y-%m-%d_%H-%M.ts`.
"""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass, field
from pathlib import Path

from sintonizador.archiver.config import ArchiveTarget
from sintonizador.videorec.config import pretty_name, safe_name

log = logging.getLogger(__name__)

_RESTART_DELAY_S = 1.0  # más alto que CC (0.5s): respawnear NVENC es más caro


@dataclass
class PipelineStats:
    """Métricas runtime de una pipeline de grabación — visibles vía /videorec."""

    started_at: float = field(default_factory=time.time)
    bytes_to_ffmpeg: int = 0
    ffmpeg_restarts: int = 0
    last_error: str | None = None
    current_segment_path: str | None = None
    encoder_used: str = "nvenc"


def _build_ffmpeg_cmd(
    encoder: str,
    service_id: int,
    out_dir: Path,
    file_stem: str,
    bitrate: str,
    maxrate: str,
    bufsize: str,
    rotation_minutes: int,
) -> list[str]:
    """Arma el comando ffmpeg. Lee el mux crudo por stdin, igual que
    `monitor/transcode.py._build_ffmpeg_cmd` — acá cambia la salida: en vez de
    HLS en vivo, segmentos .ts rotativos a disco."""
    base = ["ffmpeg", "-hide_banner", "-loglevel", "warning", "-fflags", "+genpts"]
    inp = ["-i", "pipe:0", "-map", f"0:p:{service_id}", "-sn"]
    # Desentrelaza el 1080i de broadcast a progresivo — corre en CPU en ambos
    # encoders, costo bajo, el i9-14900 lo absorbe sin problema.
    vf = ["-vf", "bwdif=mode=1:parity=auto:deint=all"]
    if encoder == "nvenc":
        venc = [
            "-c:v", "h264_nvenc", "-preset", "p4", "-tune", "ll", "-rc", "vbr",
            "-cq", "23", "-b:v", bitrate, "-maxrate", maxrate, "-bufsize", bufsize,
            "-g", "60",
        ]
    else:  # software (fallback CPU)
        venc = [
            "-c:v", "libx264", "-preset", "veryfast", "-crf", "20",
            "-maxrate", maxrate, "-bufsize", bufsize, "-g", "60",
        ]
    aenc = ["-c:a", "aac", "-b:a", "160k", "-ar", "48000", "-ac", "2"]
    out = [
        "-f", "segment", "-segment_time", str(rotation_minutes * 60),
        "-segment_atclocktime", "1", "-reset_timestamps", "1", "-strftime", "1",
        str(out_dir / f"{file_stem}_%Y-%m-%d_%H-%M.ts"),
    ]
    return base + inp + vf + venc + aenc + out


class VideoRecordPipeline:
    """`TsConsumer` que graba un subcanal a segmentos .ts rotativos."""

    def __init__(
        self,
        target: ArchiveTarget,
        channel_id: int,
        output_root: Path,
        encoder: str = "nvenc",
        bitrate: str = "5M",
        maxrate: str = "6M",
        bufsize: str = "10M",
        rotation_minutes: int = 30,
    ) -> None:
        self.target = target
        self.channel_id = channel_id
        self.output_root = Path(output_root)
        self.encoder = encoder
        self.bitrate = bitrate
        self.maxrate = maxrate
        self.bufsize = bufsize
        self.rotation_minutes = rotation_minutes
        self.stats = PipelineStats(encoder_used=encoder)

        self._safe = safe_name(pretty_name(target))
        self._dir_name = f"canal_{channel_id:02d}_{self._safe}"
        self._file_stem = self._dir_name
        self.out_dir = self.output_root / self._dir_name

        self._task: asyncio.Task | None = None
        self._pump_task: asyncio.Task | None = None
        self._stopping = asyncio.Event()
        self._proc: asyncio.subprocess.Process | None = None
        # Igual que ArchivePipeline/HlsConsumer: cola chica con drop-oldest.
        # Una pipeline lenta (ej. NVENC saturado) no debe atrasar al
        # AdapterReader ni a sus hermanas del mismo adapter.
        self._feed_queue: asyncio.Queue[bytes] = asyncio.Queue(maxsize=200)

    @property
    def key(self) -> str:
        """Identificador como `TsConsumer` dentro del adapter."""
        return f"videorec:{self.target.slug}"

    @property
    def is_active(self) -> bool:
        return self._task is not None and not self._task.done()

    @property
    def channel_name(self) -> str:
        return pretty_name(self.target)

    # --- ciclo de vida ---

    async def start(self) -> None:
        if self.is_active:
            return
        self.out_dir.mkdir(parents=True, exist_ok=True)
        self._stopping = asyncio.Event()
        self._feed_queue = asyncio.Queue(maxsize=200)
        self._task = asyncio.create_task(self._run(), name=f"videorec-{self.target.slug}")
        self._pump_task = asyncio.create_task(self._pump_loop(), name=f"videorec-pump-{self.target.slug}")

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

    # --- fan-out: lo llama el MuxReader del adapter con bytes del TS crudo ---

    def feed_ts(self, data: bytes) -> None:
        if self._stopping.is_set():
            return
        try:
            self._feed_queue.put_nowait(data)
        except asyncio.QueueFull:
            try:
                self._feed_queue.get_nowait()  # drop oldest
                self._feed_queue.put_nowait(data)
                self.stats.last_error = "buffer full — chunks dropped"
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
                continue  # ffmpeg restartando — chunk se pierde, normal
            try:
                proc.stdin.write(data)
                await proc.stdin.drain()
                self.stats.bytes_to_ffmpeg += len(data)
            except (BrokenPipeError, ConnectionResetError):
                pass  # ffmpeg murió — el supervisor lo restart

    # --- internals: supervisor de ffmpeg ---

    async def _run(self) -> None:
        log.info("videorec %s: starting (adapter=%d program=%d encoder=%s)",
                 self.target.slug, self.target.adapter, self.target.program_id, self.encoder)
        while not self._stopping.is_set():
            try:
                await self._run_one_lifetime()
            except asyncio.CancelledError:
                raise
            except Exception as e:
                log.exception("videorec %s: error in lifetime", self.target.slug)
                self.stats.last_error = str(e)
            if self._stopping.is_set():
                break
            self.stats.ffmpeg_restarts += 1
            log.warning("videorec %s: ffmpeg murió — restart #%d en %.1fs",
                        self.target.slug, self.stats.ffmpeg_restarts, _RESTART_DELAY_S)
            try:
                await asyncio.wait_for(self._stopping.wait(), timeout=_RESTART_DELAY_S)
            except asyncio.TimeoutError:
                pass
        log.info("videorec %s: stopped", self.target.slug)

    async def _run_one_lifetime(self) -> None:
        self.stats.current_segment_path = str(self.out_dir / f"{self._file_stem}_*.ts")
        cmd = _build_ffmpeg_cmd(
            encoder=self.encoder,
            service_id=self.target.program_id,
            out_dir=self.out_dir,
            file_stem=self._file_stem,
            bitrate=self.bitrate,
            maxrate=self.maxrate,
            bufsize=self.bufsize,
            rotation_minutes=self.rotation_minutes,
        )
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.PIPE,
        )
        self._proc = proc
        log.info("videorec %s: ffmpeg pid=%d", self.target.slug, proc.pid)
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
        """Drena stderr; loggea y guarda el último error para /videorec."""
        assert proc.stderr is not None
        lines_count = 0
        tail: list[str] = []
        try:
            while not self._stopping.is_set():
                line = await proc.stderr.readline()
                if not line:
                    break
                lines_count += 1
                decoded = line.decode(errors="replace").rstrip()
                if lines_count <= 3:
                    log.info("ffmpeg[%s] init: %s", self.target.slug, decoded)
                else:
                    tail.append(decoded)
                    if len(tail) > 5:
                        tail.pop(0)
                if decoded:
                    self.stats.last_error = decoded
        finally:
            for ln in tail:
                log.info("ffmpeg[%s] last: %s", self.target.slug, ln)
