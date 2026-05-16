"""Sesión de extracción de Closed Captions en vivo.

Pipeline:
    demux (TSDEMUX_TAP) → ccextractor stdin → ccextractor stdout (SRT) → parser → pubsub

Una sesión por adapter. Ref-counted: arranca cuando aparece el primer cliente
WS, se apaga cuando el último se desconecta.
"""

from __future__ import annotations

import asyncio
import logging
import os
import shutil
from dataclasses import dataclass, field

from sintonizador.dvb.demux import Demux

log = logging.getLogger(__name__)


_CHUNK = 188 * 200


@dataclass
class LiveCCSession:
    adapter: int
    subscribers: list[asyncio.Queue] = field(default_factory=list)
    _task: asyncio.Task | None = None
    _stopping: asyncio.Event = field(default_factory=asyncio.Event)
    _lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    _last_error: str | None = None

    @property
    def is_active(self) -> bool:
        return self._task is not None and not self._task.done()

    async def subscribe(self) -> asyncio.Queue:
        async with self._lock:
            q: asyncio.Queue = asyncio.Queue(maxsize=200)
            self.subscribers.append(q)
            if not self.is_active:
                self._stopping = asyncio.Event()
                self._task = asyncio.create_task(
                    self._pump(), name=f"live-cc-{self.adapter}"
                )
            # Si ya hubo error en una sesión previa, comunicarlo al nuevo cliente
            if self._last_error:
                await q.put({"type": "error", "message": self._last_error})
            return q

    async def unsubscribe(self, q: asyncio.Queue) -> None:
        async with self._lock:
            if q in self.subscribers:
                self.subscribers.remove(q)
            if not self.subscribers and self.is_active:
                log.info("live-cc adapter %d: último cliente fuera, parando", self.adapter)
                self._stopping.set()
                try:
                    await asyncio.wait_for(self._task, timeout=3)
                except asyncio.TimeoutError:
                    log.warning("live-cc adapter %d: timeout al parar", self.adapter)
                    self._task.cancel()
                self._task = None

    # --- internals ---

    async def _pump(self) -> None:
        if shutil.which("ccextractor") is None:
            self._last_error = "ccextractor no instalado (sudo apt install ccextractor)"
            await self._broadcast({"type": "error", "message": self._last_error})
            return

        dmx = Demux(adapter=self.adapter)
        proc: asyncio.subprocess.Process | None = None
        try:
            try:
                dmx.open()
                dmx.set_filter_all_pids_tsdemux_tap(buffer_kb=512)
                os.set_blocking(dmx.fd, False)
            except Exception as e:
                self._last_error = f"demux: {e}"
                await self._broadcast({"type": "error", "message": self._last_error})
                return

            # ccextractor en modo pipe: TS por stdin, SRT por stdout, sin colores.
            # Flag names en v0.94: --nofontcolor (un guion seguido) y --norollup.
            #
            # stdbuf -o0 -e0: desactiva buffering en stdout/stderr de ccextractor.
            # Sin esto, libc block-buffferea pipes (~4 KB) y no vemos eventos
            # hasta que se acumulen — para live no sirve.
            # `-1` fuerza el track CEA-608 CC1 — para broadcasters mexicanos
            # (al menos GDL) entrega texto en UTF-8 limpio. El default mezcla
            # CEA-608 + CEA-708 y los frames 708 llegan con encoding roto.
            # Verificado contra los 14 multiplexes el 2026-05-13.
            cmd = [
                "stdbuf", "-o0", "-e0",
                "ccextractor",
                "-in=ts",
                "-stdin",
                "-out=srt",
                "-stdout",
                "-quiet",
                "-1",
                "--nofontcolor",
                "--norollup",
            ]
            try:
                proc = await asyncio.create_subprocess_exec(
                    *cmd,
                    stdin=asyncio.subprocess.PIPE,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                )
            except FileNotFoundError:
                self._last_error = "ccextractor no encontrado"
                await self._broadcast({"type": "error", "message": self._last_error})
                return

            log.info("live-cc adapter %d: ccextractor pid=%d arrancado", self.adapter, proc.pid)
            await self._broadcast({"type": "status", "state": "running", "adapter": self.adapter})

            # Tres tareas concurrentes
            await asyncio.gather(
                self._ts_feeder(dmx, proc),
                self._cc_reader(proc),
                self._stderr_drain(proc),
                return_exceptions=True,
            )
        finally:
            if proc is not None and proc.returncode is None:
                try:
                    proc.terminate()
                    await asyncio.wait_for(proc.wait(), timeout=1.5)
                except (asyncio.TimeoutError, ProcessLookupError):
                    try:
                        proc.kill()
                    except ProcessLookupError:
                        pass
            dmx.close()
            await self._broadcast({"type": "status", "state": "stopped", "adapter": self.adapter})
            log.info("live-cc adapter %d: stopped", self.adapter)

    async def _ts_feeder(self, dmx: Demux, proc: asyncio.subprocess.Process) -> None:
        """Lee TS del demux fd, lo pipea al stdin de ccextractor."""
        loop = asyncio.get_running_loop()
        try:
            while not self._stopping.is_set() and proc.returncode is None:
                try:
                    data = await loop.run_in_executor(None, _read_or_empty, dmx.fd)
                except OSError as e:
                    log.warning("live-cc adapter %d: read TS error: %s", self.adapter, e)
                    break
                if data:
                    try:
                        proc.stdin.write(data)  # type: ignore[union-attr]
                        await proc.stdin.drain()  # type: ignore[union-attr]
                    except (BrokenPipeError, ConnectionResetError):
                        break
                else:
                    await asyncio.sleep(0.05)
        finally:
            try:
                if proc.stdin and not proc.stdin.is_closing():
                    proc.stdin.close()
            except Exception:
                pass

    async def _cc_reader(self, proc: asyncio.subprocess.Process) -> None:
        """Lee stdout de ccextractor línea por línea, parsea bloques SRT, emite eventos."""
        assert proc.stdout is not None
        block: list[str] = []
        try:
            while not self._stopping.is_set():
                raw = await proc.stdout.readline()
                if not raw:
                    break  # EOF
                line = raw.decode("utf-8", errors="replace").rstrip("\r\n")
                if line == "":
                    if block:
                        ev = _parse_srt_block(block)
                        if ev:
                            await self._broadcast(ev)
                        block = []
                else:
                    block.append(line)
            # flush final
            if block:
                ev = _parse_srt_block(block)
                if ev:
                    await self._broadcast(ev)
        except Exception:
            log.exception("live-cc adapter %d: cc_reader exploded", self.adapter)

    async def _stderr_drain(self, proc: asyncio.subprocess.Process) -> None:
        """Drena stderr de ccextractor (sino el pipe se llena y bloquea)."""
        assert proc.stderr is not None
        while not self._stopping.is_set():
            line = await proc.stderr.readline()
            if not line:
                break
            log.debug("ccextractor stderr: %s", line.decode(errors="replace").rstrip())

    async def _broadcast(self, event: dict) -> None:
        # Si el queue está lleno, descartar oldest para preferir frescos
        for q in list(self.subscribers):
            if q.full():
                try:
                    q.get_nowait()
                except asyncio.QueueEmpty:
                    pass
            try:
                q.put_nowait(event)
            except asyncio.QueueFull:
                pass


def _read_or_empty(fd: int) -> bytes:
    try:
        return os.read(fd, _CHUNK)
    except BlockingIOError:
        return b""


def _parse_srt_block(lines: list[str]) -> dict | None:
    """Parsea un bloque SRT: [seq, 'HH:MM:SS,mmm --> HH:MM:SS,mmm', *text]."""
    if len(lines) < 2:
        return None
    try:
        seq = int(lines[0])
    except ValueError:
        return None
    tc = lines[1]
    if " --> " not in tc:
        return None
    a, b = tc.split(" --> ", 1)
    try:
        start_ms = _tc_to_ms(a)
        end_ms = _tc_to_ms(b)
    except (ValueError, IndexError):
        return None
    text = "\n".join(lines[2:])
    return {
        "type": "cc",
        "seq": seq,
        "start_ms": start_ms,
        "end_ms": end_ms,
        "text": text,
    }


def _tc_to_ms(tc: str) -> int:
    """'HH:MM:SS,mmm' → milisegundos."""
    h, m, rest = tc.strip().split(":")
    s, ms = rest.split(",")
    return (int(h) * 3600 + int(m) * 60 + int(s)) * 1000 + int(ms)


class LiveCCManager:
    """Singleton-ish container que mapea adapter → sesión."""

    def __init__(self) -> None:
        self.sessions: dict[int, LiveCCSession] = {}

    def get(self, adapter: int) -> LiveCCSession:
        if adapter not in self.sessions:
            self.sessions[adapter] = LiveCCSession(adapter=adapter)
        return self.sessions[adapter]

    async def shutdown(self) -> None:
        """Para todas las sesiones activas (llamar en lifespan teardown)."""
        for s in self.sessions.values():
            s._stopping.set()
            if s._task is not None and not s._task.done():
                try:
                    await asyncio.wait_for(s._task, timeout=2)
                except asyncio.TimeoutError:
                    s._task.cancel()
        self.sessions.clear()
