"""Una `ArchivePipeline` por subcanal: ccextractor + archivos rotativos.

NO maneja demux directamente — recibe bytes vía `feed_ts()` del `AdapterReader`
del adapter. Esto evita la duplicación de paquetes del kernel cuando hay N
subcanales en el mismo adapter.

Pipeline:
    AdapterReader (demux compartido)
       ↓ feed_ts(bytes)
    stdin de ccextractor -in=ts -stdin -pn N -1 -out=srt -stdout
       ↓ stdout
    parser SRT block-by-block
       ↓
    .srt + .txt rotativos cada `rotation_minutes` (default 30)

Auto-restart de ccextractor si crashea.
"""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

from sintonizador.archiver.config import ArchiveTarget

log = logging.getLogger(__name__)


_RESTART_DELAY_S = 0.5


@dataclass
class PipelineStats:
    """Métricas runtime de una pipeline — visibles vía /archive/status."""

    started_at: float = field(default_factory=time.time)
    blocks_written: int = 0
    bytes_to_ccextractor: int = 0
    last_event_time: float | None = None
    ccextractor_restarts: int = 0
    current_period_start: datetime | None = None
    current_srt_path: str | None = None
    current_txt_path: str | None = None
    last_error: str | None = None


def current_period_start(rotation_minutes: int, now: datetime | None = None) -> datetime:
    now = now or datetime.now()
    if rotation_minutes >= 60:
        return now.replace(minute=0, second=0, microsecond=0)
    minute = (now.minute // rotation_minutes) * rotation_minutes
    return now.replace(minute=minute, second=0, microsecond=0)


def file_paths(archive_root: Path, slug: str, period: datetime) -> tuple[Path, Path]:
    date_str = period.strftime("%Y-%m-%d")
    time_str = period.strftime("%H-%M")
    dir_path = archive_root / slug
    return (
        dir_path / f"{date_str}_{time_str}.srt",
        dir_path / f"{date_str}_{time_str}.txt",
    )


class ArchivePipeline:
    def __init__(
        self,
        target: ArchiveTarget,
        archive_root: Path,
        rotation_minutes: int = 30,
    ) -> None:
        self.target = target
        self.archive_root = archive_root
        self.rotation_minutes = rotation_minutes
        self.stats = PipelineStats()
        self._task: asyncio.Task | None = None
        self._stopping = asyncio.Event()
        self._proc: asyncio.subprocess.Process | None = None
        # Cola de chunks TS pendientes de escribir al stdin de ccextractor.
        # maxsize=50 chunks × 37.6 KB ≈ 1.9 MB de backlog max per pipeline.
        # Si la pipeline se atrasa (ccextractor lento), descartamos chunks
        # viejos en feed_ts; cada pipeline drena a su propio ritmo SIN
        # afectar a sus hermanas del mismo adapter.
        self._feed_queue: asyncio.Queue[bytes] = asyncio.Queue(maxsize=50)
        self._pump_task: asyncio.Task | None = None
        # Subscribers (WS /ws/archive/{slug})
        self._subscribers: list[asyncio.Queue] = []

    @property
    def is_active(self) -> bool:
        return self._task is not None and not self._task.done()

    # --- pubsub (WS /ws/archive/{slug}) ---

    def subscribe(self) -> asyncio.Queue:
        q: asyncio.Queue = asyncio.Queue(maxsize=200)
        self._subscribers.append(q)
        return q

    def unsubscribe(self, q: asyncio.Queue) -> None:
        if q in self._subscribers:
            self._subscribers.remove(q)

    def _broadcast(self, event: dict) -> None:
        if not self._subscribers:
            return
        for q in list(self._subscribers):
            if q.full():
                try: q.get_nowait()
                except asyncio.QueueEmpty: pass
            try: q.put_nowait(event)
            except asyncio.QueueFull: pass

    # --- ciclo de vida ---

    async def start(self) -> None:
        if self.is_active:
            return
        self._stopping = asyncio.Event()
        # Re-crear queue (en caso de restart, descartar backlog viejo)
        self._feed_queue = asyncio.Queue(maxsize=50)
        self._task = asyncio.create_task(self._run(), name=f"archive-{self.target.slug}")
        self._pump_task = asyncio.create_task(self._pump_loop(), name=f"pump-{self.target.slug}")

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

    # --- fan-out: lo llama AdapterReader con bytes del TS ---

    def feed_ts(self, data: bytes) -> None:
        """Fan-in síncrono del AdapterReader: encola en la cola interna.

        Si la cola está llena (pipeline lenta), descartamos el chunk MÁS
        VIEJO y metemos el nuevo — mantenemos latencia baja prefiriendo
        captions recientes a captions retrasados.
        """
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
        """Drena la cola → stdin del ccextractor con backpressure (drain).

        Cada pipeline tiene su propio pump, así una pipeline lenta
        retrasa su propio drain pero no afecta al AdapterReader ni a sus
        hermanas del mismo adapter.
        """
        while not self._stopping.is_set():
            try:
                data = await asyncio.wait_for(self._feed_queue.get(), timeout=1.0)
            except asyncio.TimeoutError:
                continue
            proc = self._proc
            if proc is None or proc.stdin is None or proc.stdin.is_closing():
                continue  # ccextractor restartando — chunk se pierde, normal
            try:
                proc.stdin.write(data)
                await proc.stdin.drain()  # backpressure SOLO a esta pipeline
                self.stats.bytes_to_ccextractor += len(data)
            except (BrokenPipeError, ConnectionResetError):
                pass  # ccextractor murió — el supervisor lo restart

    # --- internals: supervisor de ccextractor ---

    async def _run(self) -> None:
        log.info("pipeline %s: starting (adapter=%d program=%d)",
                 self.target.slug, self.target.adapter, self.target.program_id)
        while not self._stopping.is_set():
            try:
                await self._run_one_lifetime()
            except asyncio.CancelledError:
                raise
            except Exception as e:
                log.exception("pipeline %s: error in lifetime", self.target.slug)
                self.stats.last_error = str(e)
            if self._stopping.is_set():
                break
            self.stats.ccextractor_restarts += 1
            log.warning("pipeline %s: ccextractor murió — restart #%d en %.1fs",
                        self.target.slug, self.stats.ccextractor_restarts, _RESTART_DELAY_S)
            try:
                await asyncio.wait_for(self._stopping.wait(), timeout=_RESTART_DELAY_S)
            except asyncio.TimeoutError:
                pass
        log.info("pipeline %s: stopped", self.target.slug)

    async def _run_one_lifetime(self) -> None:
        """Una vida de ccextractor. Sale cuando el proc termina (stdout EOF)."""
        proc = await self._spawn_ccextractor()
        self._proc = proc
        try:
            # cc_reader y stderr_drain corren en paralelo. Cuando ccextractor
            # muere, stdout y stderr se cierran, ambos tasks terminan.
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
            "-in=ts",
            "-stdin",
            "-out=srt",
            "-stdout",
            "-quiet",
            "-s",  # stream mode — no terminar en EOF aparente
            "-1",  # CC1 (CEA-608) — texto limpio UTF-8 para broadcasters MX
            "-pn", str(self.target.program_id),
            "--nofontcolor",
            "--norollup",
        ]
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        log.info("pipeline %s: ccextractor pid=%d arrancado",
                 self.target.slug, proc.pid)
        return proc

    async def _cc_reader(self, proc: asyncio.subprocess.Process) -> None:
        """Lee SRT del stdout, parsea bloques, escribe archivos del período actual.

        Con timeout sobre readline para que rotación pase incluso si no hay
        captions (subcanales silenciosos como 2.2/3.3/3.4).
        """
        assert proc.stdout is not None
        block: list[str] = []
        state: dict = {
            "srt_file": None,
            "txt_file": None,
            "period": None,
            "seq_in_file": 0,
        }

        def ensure_period_open() -> None:
            period_now = current_period_start(self.rotation_minutes)
            if period_now == state["period"]:
                return
            for k in ("srt_file", "txt_file"):
                if state[k] is not None:
                    try: state[k].close()
                    except Exception: pass
                    state[k] = None
            state["period"] = period_now
            self.stats.current_period_start = period_now
            srt_path, txt_path = file_paths(self.archive_root, self.target.slug, period_now)
            srt_path.parent.mkdir(parents=True, exist_ok=True)
            state["srt_file"] = open(srt_path, "a", encoding="utf-8", buffering=1)
            state["txt_file"] = open(txt_path, "a", encoding="utf-8", buffering=1)
            self.stats.current_srt_path = str(srt_path)
            self.stats.current_txt_path = str(txt_path)
            state["seq_in_file"] = 0
            state["txt_file"].write(
                f"# {self.target.slug} · period {period_now.isoformat()} "
                f"· program {self.target.program_id} · "
                f"{self.target.frequency_hz/1e6:.3f} MHz\n"
            )

        try:
            ensure_period_open()
            while not self._stopping.is_set():
                try:
                    raw = await asyncio.wait_for(proc.stdout.readline(), timeout=2.0)
                except asyncio.TimeoutError:
                    ensure_period_open()
                    continue
                if not raw:
                    break
                line = raw.decode("utf-8", errors="replace").rstrip("\r\n")
                ensure_period_open()
                if line == "":
                    if block:
                        parsed = _parse_srt_block(block)
                        srt_f = state["srt_file"]; txt_f = state["txt_file"]
                        if parsed and srt_f is not None and txt_f is not None:
                            state["seq_in_file"] += 1
                            self._write_block(srt_f, txt_f, state["seq_in_file"], parsed, block)
                            self.stats.blocks_written += 1
                            self.stats.last_event_time = time.time()
                        block = []
                else:
                    block.append(line)
        finally:
            for k in ("srt_file", "txt_file"):
                if state[k] is not None:
                    try: state[k].close()
                    except Exception: pass

    def _write_block(
        self,
        srt_file,
        txt_file,
        seq: int,
        parsed: dict,
        raw_block: list[str],
    ) -> None:
        # SRT verbatim
        srt_file.write(f"{seq}\n")
        srt_file.write(f"{raw_block[1]}\n")
        for textline in raw_block[2:]:
            srt_file.write(f"{textline}\n")
        srt_file.write("\n")
        # TXT
        now = datetime.now()
        ts = now.strftime("%H:%M:%S")
        text = " ".join(t.strip() for t in raw_block[2:] if t.strip())
        while "  " in text:
            text = text.replace("  ", " ")
        if text:
            txt_file.write(f"[{ts}] {text}\n")
            duration_ms = _block_duration_ms(raw_block[1])
            self._broadcast({
                "type": "cc",
                "slug": self.target.slug,
                "wall_clock": now.isoformat(timespec="seconds"),
                "wall_clock_short": ts,
                "seq": seq,
                "timecode": raw_block[1],
                "duration_ms": duration_ms,
                "text": text,
            })

    async def _stderr_drain(self, proc: asyncio.subprocess.Process) -> None:
        """Drena stderr; loggea las primeras y últimas líneas a INFO."""
        assert proc.stderr is not None
        lines_count = 0
        tail = []
        try:
            while not self._stopping.is_set():
                line = await proc.stderr.readline()
                if not line:
                    break
                lines_count += 1
                decoded = line.decode(errors="replace").rstrip()
                if lines_count <= 3:
                    log.info("ccextractor[%s] init: %s", self.target.slug, decoded)
                else:
                    tail.append(decoded)
                    if len(tail) > 5:
                        tail.pop(0)
        finally:
            if tail:
                for ln in tail:
                    log.info("ccextractor[%s] last: %s", self.target.slug, ln)


def _parse_srt_block(lines: list[str]) -> dict | None:
    if len(lines) < 2 or " --> " not in lines[1]:
        return None
    try:
        seq = int(lines[0])
    except ValueError:
        return None
    return {"seq": seq, "timecode": lines[1], "text_lines": lines[2:]}


def _tc_to_ms(tc: str) -> int:
    h, m, rest = tc.strip().split(":")
    s, ms = rest.split(",")
    return (int(h) * 3600 + int(m) * 60 + int(s)) * 1000 + int(ms)


def _block_duration_ms(timecode_line: str) -> int:
    if " --> " not in timecode_line:
        return 0
    try:
        a, b = timecode_line.split(" --> ", 1)
        return max(0, _tc_to_ms(b) - _tc_to_ms(a))
    except (ValueError, IndexError):
        return 0
