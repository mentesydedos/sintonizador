"""CC en vivo por subcanal, on-demand, sin escribir a disco.

`LiveCCConsumer` es un `TsConsumer` (se cuelga del MuxReader compartido del
adapter → sin segundo tap) que corre `ccextractor -pn {program_id}` y emite
los captions por pubsub. Es el `ArchivePipeline` sin la parte de archivos.

`SubchannelCCManager` ref-cuenta consumidores por slug: crea+attachea el
consumidor cuando aparece el primer cliente WS y lo detacha cuando se va el
último. Para subcanales que YA cubre el archiver 24×7, el endpoint reusa la
pipeline del archiver en vez de crear otro ccextractor.
"""

from __future__ import annotations

import asyncio
import logging
import time
from datetime import datetime

from sintonizador.archiver.pipeline import _block_duration_ms, _parse_srt_block
from sintonizador.mux import MuxReaderRegistry

log = logging.getLogger(__name__)

_RESTART_DELAY_S = 0.5


class LiveCCConsumer:
    """`TsConsumer` que extrae CC de UN programa y los publica (sin archivos)."""

    def __init__(self, slug: str, program_id: int) -> None:
        self.slug = slug
        self.program_id = program_id
        self._feed_queue: asyncio.Queue[bytes] = asyncio.Queue(maxsize=50)
        self._subscribers: list[asyncio.Queue] = []
        self._task: asyncio.Task | None = None
        self._pump_task: asyncio.Task | None = None
        self._stopping = asyncio.Event()
        self._proc: asyncio.subprocess.Process | None = None
        self.last_cc_at: float | None = None

    @property
    def key(self) -> str:
        return f"cc:{self.slug}"

    @property
    def subscribers(self) -> list[asyncio.Queue]:
        return self._subscribers

    # --- pubsub ---

    def subscribe(self) -> asyncio.Queue:
        q: asyncio.Queue = asyncio.Queue(maxsize=200)
        self._subscribers.append(q)
        return q

    def unsubscribe(self, q: asyncio.Queue) -> None:
        if q in self._subscribers:
            self._subscribers.remove(q)

    def _broadcast(self, event: dict) -> None:
        for q in list(self._subscribers):
            if q.full():
                try: q.get_nowait()
                except asyncio.QueueEmpty: pass
            try: q.put_nowait(event)
            except asyncio.QueueFull: pass

    # --- ciclo de vida (lo arranca/para el MuxReader vía add/remove_consumer) ---

    async def start(self) -> None:
        if self._task is not None and not self._task.done():
            return
        self._stopping = asyncio.Event()
        self._feed_queue = asyncio.Queue(maxsize=50)
        self._task = asyncio.create_task(self._run(), name=f"livecc-{self.slug}")
        self._pump_task = asyncio.create_task(self._pump_loop(), name=f"livecc-pump-{self.slug}")

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

    # --- fan-in del MuxReader ---

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

    # --- supervisor de ccextractor ---

    async def _run(self) -> None:
        log.info("livecc %s: starting (program=%d)", self.slug, self.program_id)
        while not self._stopping.is_set():
            try:
                await self._run_one_lifetime()
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("livecc %s: error en lifetime", self.slug)
            if self._stopping.is_set():
                break
            try:
                await asyncio.wait_for(self._stopping.wait(), timeout=_RESTART_DELAY_S)
            except asyncio.TimeoutError:
                pass
        log.info("livecc %s: stopped", self.slug)

    async def _run_one_lifetime(self) -> None:
        proc = await self._spawn_ccextractor()
        self._proc = proc
        try:
            await asyncio.gather(
                self._cc_reader(proc),
                self._stderr_drain(proc),
                return_exceptions=True,
            )
        finally:
            self._proc = None
            if proc.returncode is None:
                try:
                    proc.terminate()
                    await asyncio.wait_for(proc.wait(), timeout=2.0)
                except (asyncio.TimeoutError, ProcessLookupError):
                    try: proc.kill()
                    except ProcessLookupError: pass

    async def _spawn_ccextractor(self) -> asyncio.subprocess.Process:
        cmd = [
            "stdbuf", "-o0", "-e0",
            "ccextractor",
            "-in=ts", "-stdin", "-out=srt", "-stdout",
            "-quiet", "-s", "-1",
            "-pn", str(self.program_id),
            "--nofontcolor", "--norollup",
        ]
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        log.info("livecc %s: ccextractor pid=%d", self.slug, proc.pid)
        return proc

    async def _cc_reader(self, proc: asyncio.subprocess.Process) -> None:
        assert proc.stdout is not None
        block: list[str] = []
        seq = 0
        while not self._stopping.is_set():
            try:
                raw = await asyncio.wait_for(proc.stdout.readline(), timeout=2.0)
            except asyncio.TimeoutError:
                continue
            if not raw:
                break
            line = raw.decode("utf-8", errors="replace").rstrip("\r\n")
            if line == "":
                if block:
                    parsed = _parse_srt_block(block)
                    if parsed:
                        text = " ".join(t.strip() for t in block[2:] if t.strip())
                        while "  " in text:
                            text = text.replace("  ", " ")
                        if text:
                            seq += 1
                            now = datetime.now()
                            self.last_cc_at = time.time()
                            self._broadcast({
                                "type": "cc",
                                "slug": self.slug,
                                "seq": seq,
                                "wall_clock": now.isoformat(timespec="seconds"),
                                "wall_clock_short": now.strftime("%H:%M:%S"),
                                "timecode": block[1],
                                "duration_ms": _block_duration_ms(block[1]),
                                "text": text,
                            })
                    block = []
            else:
                block.append(line)

    async def _stderr_drain(self, proc: asyncio.subprocess.Process) -> None:
        assert proc.stderr is not None
        while not self._stopping.is_set():
            line = await proc.stderr.readline()
            if not line:
                break
            log.debug("ccextractor[%s] stderr: %s", self.slug, line.decode(errors="replace").rstrip())


class SubchannelCCManager:
    """Ref-cuenta `LiveCCConsumer` por slug; attach/detach al MuxReader."""

    def __init__(self, registry: MuxReaderRegistry) -> None:
        self.registry = registry
        self._consumers: dict[str, LiveCCConsumer] = {}
        self._adapter: dict[str, int] = {}
        self._lock = asyncio.Lock()

    def get(self, slug: str) -> LiveCCConsumer | None:
        return self._consumers.get(slug)

    async def subscribe(self, slug: str, adapter: int, program_id: int) -> tuple[LiveCCConsumer, asyncio.Queue]:
        async with self._lock:
            consumer = self._consumers.get(slug)
            if consumer is None:
                consumer = LiveCCConsumer(slug=slug, program_id=program_id)
                await self.registry.attach(adapter, consumer)
                self._consumers[slug] = consumer
                self._adapter[slug] = adapter
            return consumer, consumer.subscribe()

    async def unsubscribe(self, slug: str, q: asyncio.Queue) -> None:
        async with self._lock:
            consumer = self._consumers.get(slug)
            if consumer is None:
                return
            consumer.unsubscribe(q)
            if not consumer.subscribers:
                adapter = self._adapter.pop(slug, None)
                self._consumers.pop(slug, None)
                if adapter is not None:
                    await self.registry.detach(adapter, consumer.key)

    async def teardown(self) -> None:
        async with self._lock:
            items = list(self._consumers.items())
            self._consumers.clear()
            self._adapter.clear()
        for slug, consumer in items:
            # el detach del registry lo hace stop_all en el lifespan; acá solo
            # marcamos parada por las dudas
            consumer._stopping.set()
