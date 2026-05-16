"""Manager para escaneo de canales en background.

Corre `scripts/scan-channels.sh <adapter>` como subprocess y expone status
para que la UI haga polling. Solo una sesión simultánea por adapter, y
nunca sobre un adapter reservado por el archiver.

Después de un scan exitoso, channels.conf en disco queda actualizado;
los consumidores (api/app.py) deben recargarlo manualmente con
_load_catalog() si quieren ver los nuevos canales.
"""

from __future__ import annotations

import asyncio
import logging
import re
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal

log = logging.getLogger(__name__)


ScanState = Literal["idle", "running", "done", "error"]


@dataclass
class ScanSession:
    adapter: int
    state: ScanState = "idle"
    started_at: float | None = None
    finished_at: float | None = None
    progress: str | None = None  # human-readable, ej. "frec 23/52"
    channels_found: int | None = None
    error: str | None = None
    log_path: Path | None = None
    _task: asyncio.Task | None = field(default=None, repr=False)


class ScanManager:
    """Singleton-ish: una sesión por adapter."""

    def __init__(self, script_path: Path, channels_conf_path: Path) -> None:
        self.script_path = script_path
        self.channels_conf_path = channels_conf_path
        self.sessions: dict[int, ScanSession] = {}

    def status(self, adapter: int) -> ScanSession:
        return self.sessions.get(adapter, ScanSession(adapter=adapter))

    async def start(self, adapter: int) -> ScanSession:
        existing = self.sessions.get(adapter)
        if existing and existing.state == "running":
            return existing
        if not self.script_path.exists():
            return ScanSession(
                adapter=adapter, state="error",
                error=f"script no existe: {self.script_path}",
            )
        log_dir = Path("/tmp")
        log_path = log_dir / f"sint-scan-adapter{adapter}-{int(time.time())}.log"
        session = ScanSession(
            adapter=adapter,
            state="running",
            started_at=time.time(),
            log_path=log_path,
        )
        self.sessions[adapter] = session
        session._task = asyncio.create_task(
            self._run(session), name=f"scan-adapter{adapter}"
        )
        return session

    async def _run(self, session: ScanSession) -> None:
        log.info("scan adapter %d: arrancando (log=%s)", session.adapter, session.log_path)
        try:
            # scan-channels.sh espera el adapter como argumento posicional
            # y sobrescribe channels.conf en el root del proyecto.
            proc = await asyncio.create_subprocess_exec(
                "bash",
                str(self.script_path),
                str(session.adapter),
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.STDOUT,
            )
            assert proc.stdout is not None
            with open(session.log_path, "wb") as logf:  # type: ignore[arg-type]
                while True:
                    line = await proc.stdout.readline()
                    if not line:
                        break
                    logf.write(line)
                    logf.flush()
                    decoded = line.decode(errors="replace").strip()
                    # Heurística: dvbv5-scan imprime "Scanning frequency #N FREQ"
                    m = re.search(r"Scanning frequency #(\d+)\s+(\d+)", decoded)
                    if m:
                        session.progress = f"frec #{m.group(1)} ({int(m.group(2))/1e6:.0f} MHz)"
                    elif "Found channels" in decoded:
                        # Línea final que cuenta cuántos canales se encontraron
                        m2 = re.search(r"Found channels.*?(\d+)", decoded)
                        if m2:
                            session.channels_found = int(m2.group(1))
            await proc.wait()
            if proc.returncode != 0:
                session.state = "error"
                session.error = f"scan-channels.sh exit {proc.returncode}"
            else:
                # Contar [CHANNEL] en channels.conf como respaldo
                if self.channels_conf_path.exists() and session.channels_found is None:
                    try:
                        session.channels_found = sum(
                            1 for ln in self.channels_conf_path.read_text(
                                encoding="utf-8", errors="replace"
                            ).splitlines()
                            if ln.startswith("[")
                        )
                    except OSError:
                        pass
                session.state = "done"
            session.finished_at = time.time()
            log.info("scan adapter %d: %s · canales=%s",
                     session.adapter, session.state, session.channels_found)
        except Exception as e:
            log.exception("scan adapter %d: excepción", session.adapter)
            session.state = "error"
            session.error = str(e)
            session.finished_at = time.time()
