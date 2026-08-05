"""Stream MPEG-TS de UN solo programa (subcanal) del mux, vía el fan-out compartido.

`ProgramTsConsumer` es un `TsConsumer`: se cuelga del `MuxReader` del adapter
(un solo tap), alimenta un ffmpeg que filtra el programa (`-map 0:p:{service_id}
-c copy`) y reempaqueta TS, y deja la salida en una cola que lee el generador
de la respuesta HTTP. Lleva **video (para CC) + audio (para ASR)** del subcanal.

Sirve para que el transcriber externo (Qwen3-ASR + ccextractor) consuma un
subcanal local como si fuera un stream de Tvheadend. Varios subcanales del mismo
mux = varios consumidores sobre un único tap.
"""

from __future__ import annotations

import asyncio
import logging

log = logging.getLogger(__name__)

_OUT_CHUNK = 188 * 200


class ProgramTsConsumer:
    def __init__(self, key: str, service_id: int) -> None:
        self._key = key
        self.service_id = service_id
        self._feed_queue: asyncio.Queue[bytes] = asyncio.Queue(maxsize=80)
        self._out_queue: asyncio.Queue[bytes] = asyncio.Queue(maxsize=200)
        self._stopping = asyncio.Event()
        self._proc: asyncio.subprocess.Process | None = None
        self._tasks: list[asyncio.Task] = []

    @property
    def key(self) -> str:
        return self._key

    async def start(self) -> None:
        self._stopping = asyncio.Event()
        self._proc = await asyncio.create_subprocess_exec(
            "ffmpeg", "-hide_banner", "-loglevel", "error", "-fflags", "+genpts",
            "-i", "pipe:0",
            "-map", f"0:p:{self.service_id}",
            "-c", "copy", "-f", "mpegts", "pipe:1",
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        log.info("progts %s: ffmpeg pid=%d (program=%d)", self._key, self._proc.pid, self.service_id)
        self._tasks = [
            asyncio.create_task(self._pump(), name=f"progts-pump-{self._key}"),
            asyncio.create_task(self._drain_out(), name=f"progts-out-{self._key}"),
            asyncio.create_task(self._drain_err(), name=f"progts-err-{self._key}"),
        ]

    async def _drain_err(self) -> None:
        proc = self._proc
        if proc is None or proc.stderr is None:
            return
        try:
            while not self._stopping.is_set():
                line = await proc.stderr.readline()
                if not line:
                    break
                msg = line.decode(errors="replace").rstrip()
                # "Invalid frame dimensions 0x0" inunda con -c copy y es inofensivo.
                if msg and "Invalid frame dimensions" not in msg:
                    log.debug("ffmpeg[%s]: %s", self._key, msg)
        except asyncio.CancelledError:
            raise
        except Exception:
            pass

    async def stop(self) -> None:
        self._stopping.set()
        proc = self._proc
        if proc is not None and proc.returncode is None:
            try:
                proc.terminate()
                await asyncio.wait_for(proc.wait(), timeout=2.0)
            except (asyncio.TimeoutError, ProcessLookupError):
                try: proc.kill()
                except ProcessLookupError: pass
        for t in self._tasks:
            t.cancel()
        self._tasks = []
        # destrabar un lector pendiente
        try:
            self._out_queue.put_nowait(b"")
        except asyncio.QueueFull:
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

    async def read(self) -> bytes:
        """Próximo chunk de TS del programa (lo usa el generador HTTP)."""
        return await self._out_queue.get()

    async def _pump(self) -> None:
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
                break

    async def _drain_out(self) -> None:
        proc = self._proc
        if proc is None or proc.stdout is None:
            return
        try:
            while not self._stopping.is_set():
                chunk = await proc.stdout.read(_OUT_CHUNK)
                if not chunk:
                    break
                if self._out_queue.full():
                    try: self._out_queue.get_nowait()
                    except asyncio.QueueEmpty: pass
                try:
                    self._out_queue.put_nowait(chunk)
                except asyncio.QueueFull:
                    pass
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("progts %s: drain_out error", self._key)
