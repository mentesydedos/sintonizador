"""Wrappers async sobre ffprobe y ccextractor."""

from __future__ import annotations

import asyncio
import json
import logging
import shutil
from pathlib import Path
from typing import Any

log = logging.getLogger(__name__)


async def run_ffprobe(ts_path: Path) -> dict[str, Any]:
    """Ejecuta ffprobe -show_programs -show_streams sobre el TS y devuelve dict."""
    if shutil.which("ffprobe") is None:
        return {"error": "ffprobe no instalado"}
    cmd = [
        "ffprobe",
        "-hide_banner",
        "-loglevel", "error",
        "-show_format",
        "-show_programs",
        "-show_streams",
        "-of", "json",
        str(ts_path),
    ]
    return await _run_json(cmd)


async def run_ccextractor(ts_path: Path) -> dict[str, Any]:
    """Ejecuta ccextractor sobre el TS, devuelve dict con `text` (SRT crudo) y `error`.

    ccextractor saca subtítulos en formato SRT por default — fácil de mostrar.
    Si no encuentra CC en el stream (no todos los broadcasters/programs emiten),
    devuelve text="" y `error` con la indicación.
    """
    if shutil.which("ccextractor") is None:
        return {"error": "ccextractor no instalado (sudo apt install ccextractor)", "text": ""}

    srt_path = ts_path.with_suffix(".srt")
    cmd = [
        "ccextractor",
        "-quiet",      # no progreso
        "-o", str(srt_path),
        str(ts_path),
    ]
    proc = await asyncio.create_subprocess_exec(
        *cmd,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    stdout, stderr = await proc.communicate()
    if proc.returncode != 0:
        return {
            "error": f"ccextractor exit {proc.returncode}: {stderr.decode(errors='replace').strip()[:500]}",
            "text": "",
        }
    if not srt_path.exists():
        return {"error": "ccextractor no generó .srt — probablemente no hay CC en este TS", "text": ""}
    text = srt_path.read_text(encoding="utf-8", errors="replace")
    try:
        srt_path.unlink()
    except OSError:
        pass
    return {"text": text, "stderr": stderr.decode(errors="replace").strip()[:300]}


async def _run_json(cmd: list[str]) -> dict[str, Any]:
    """Corre `cmd`, parsea stdout como JSON. Si falla, devuelve {error, stderr}."""
    proc = await asyncio.create_subprocess_exec(
        *cmd,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    stdout, stderr = await proc.communicate()
    if proc.returncode != 0:
        return {
            "error": f"{cmd[0]} exit {proc.returncode}",
            "stderr": stderr.decode(errors="replace").strip()[:500],
        }
    try:
        return json.loads(stdout.decode())
    except json.JSONDecodeError as e:
        return {"error": f"JSON parse: {e}", "raw": stdout.decode(errors="replace")[:500]}
