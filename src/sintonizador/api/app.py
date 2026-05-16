"""FastAPI app: /tuners (snapshot HTTP) + /ws/stats (push WebSocket).

El `MonitorPoller` se inicializa en el `lifespan` de la app y vive lo que
dura el proceso. Cada conexión WS hace `subscribe()` al poller para recibir
batches.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import time
from contextlib import asynccontextmanager
from dataclasses import asdict
from pathlib import Path
from typing import AsyncIterator

from fastapi import FastAPI, HTTPException, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import HTMLResponse, StreamingResponse

from sintonizador.api.models import (
    ChannelsResponse,
    ChannelView,
    MultiplexesResponse,
    MultiplexView,
    StatsMessage,
    TuneRequest,
    TunerView,
)
from sintonizador.archiver import Archiver
from sintonizador.channels import Channel, channel_sort_key, parse_file
from sintonizador.scan import ScanManager
from sintonizador.dvb.demux import Demux, DemuxError
from sintonizador.dvb.frontend import FrontendError
from sintonizador.extract import LiveCCManager, capture_ts_seconds, run_ccextractor, run_ffprobe
from sintonizador.monitor import MonitorPoller, TunerSnapshot
from sintonizador.web import INDEX_PATH, WORKBENCH_PATH

# Tamaño de chunk al leer del dvr0. 188 (TS packet) × 200 = 37.6 KB.
# Más grande = menos syscalls; más chico = menor latencia.
STREAM_CHUNK_SIZE = 188 * 200

log = logging.getLogger(__name__)


def _read_with_retry(fd: int, max_attempts: int = 20) -> bytes:
    """Read non-blocking del dvr0 con reintento corto si EAGAIN.

    dvr0 puede no tener datos al instante (entre paquetes RF). Reintentamos
    con pequeño sleep hasta `max_attempts × 50ms`. Si no hay datos en 1 segundo,
    devolvemos vacío y el caller decide qué hacer.
    """
    import time as _t
    for _ in range(max_attempts):
        try:
            data = os.read(fd, STREAM_CHUNK_SIZE)
            if data:
                return data
        except BlockingIOError:
            pass
        _t.sleep(0.05)
    return b""


# Path al channels.conf — configurable por env var, default al del proyecto.
DEFAULT_CHANNELS_PATH = Path(os.environ.get("SINTONIZADOR_CHANNELS", "/home/sintonizador/channels.conf"))

# Archiver — config vía env.
DEFAULT_ARCHIVE_ROOT = Path(os.environ.get("SINTONIZADOR_ARCHIVE", "/home/sintonizador/archive"))
DEFAULT_ROTATION_MIN = int(os.environ.get("SINTONIZADOR_ROTATION_MINUTES", "30"))
# Auto-start del archiver al levantar uvicorn. true por default (24x7).
# Para arranque sin auto-start (debugging UI sin reservar tuners): "0"|"false".
AUTOSTART_ARCHIVER = os.environ.get("SINTONIZADOR_ARCHIVE_AUTOSTART", "true").lower() not in ("0", "false", "no")


def _snap_to_view(snap: TunerSnapshot) -> TunerView:
    return TunerView(**asdict(snap))


def _channel_to_view(idx: int, c: Channel) -> ChannelView:
    return ChannelView(
        id=idx,
        name=c.name,
        vchannel=c.vchannel,
        service_id=c.service_id,
        video_pid=c.video_pid,
        audio_pid=c.audio_pid,
        frequency_hz=c.frequency_hz,
        delivery_system=c.delivery_system_name,
        modulation=c.modulation_name,
    )


def _load_catalog() -> list[Channel]:
    """Lee el channels.conf del path configurado, ordenado ASC por (freq, vchannel).

    Si no existe el archivo, devuelve lista vacía.
    """
    if not DEFAULT_CHANNELS_PATH.exists():
        log.warning("channels.conf no encontrado en %s — catálogo vacío", DEFAULT_CHANNELS_PATH)
        return []
    chs = sorted(parse_file(DEFAULT_CHANNELS_PATH), key=channel_sort_key)
    log.info("catálogo cargado: %d canales desde %s (ordenado)", len(chs), DEFAULT_CHANNELS_PATH)
    return chs


@asynccontextmanager
async def _lifespan(app: FastAPI) -> AsyncIterator[None]:
    poller = MonitorPoller(adapters=[0, 1, 2, 3], interval_s=0.5)
    await poller.start()
    app.state.poller = poller
    app.state.channels = _load_catalog()
    app.state.live_cc = LiveCCManager()
    app.state.scan = ScanManager(
        script_path=Path("/home/sintonizador/scripts/scan-channels.sh"),
        channels_conf_path=DEFAULT_CHANNELS_PATH,
    )
    app.state.archiver = Archiver(
        poller=poller,
        channels=app.state.channels,
        archive_root=DEFAULT_ARCHIVE_ROOT,
        rotation_minutes=DEFAULT_ROTATION_MIN,
    )
    if AUTOSTART_ARCHIVER:
        try:
            await app.state.archiver.start()
        except Exception:
            log.exception("archiver: auto-start falló — continuando sin archiver")
    try:
        yield
    finally:
        try:
            await app.state.archiver.stop()
        except Exception:
            log.exception("archiver: stop falló durante shutdown")
        await app.state.live_cc.shutdown()
        await poller.stop()


def create_app() -> FastAPI:
    app = FastAPI(
        title="Sintonizador TBS6704",
        version="0.1.0",
        description="Control y monitoreo de los 4 tuners ATSC",
        lifespan=_lifespan,
    )

    @app.get("/", response_class=HTMLResponse)
    async def index() -> HTMLResponse:
        """Dashboard mínimo con las 4 tuner cards. Lee /ws/stats por WebSocket."""
        return HTMLResponse(INDEX_PATH.read_text(encoding="utf-8"))

    @app.get("/workbench", response_class=HTMLResponse)
    async def workbench() -> HTMLResponse:
        """Workbench single-tuner: selectores, scan, status, URL, EPG, CC live."""
        return HTMLResponse(WORKBENCH_PATH.read_text(encoding="utf-8"))

    @app.get("/tuners")
    async def list_tuners() -> StatsMessage:
        """Snapshot HTTP — útil para health checks o frontends sin WS."""
        poller: MonitorPoller = app.state.poller
        snaps = poller.current()
        return StatsMessage(
            timestamp=time.time(),
            tuners=[_snap_to_view(s) for s in snaps],
        )

    @app.get("/channels")
    async def list_channels() -> ChannelsResponse:
        """Catálogo cargado al iniciar (de `channels.conf`), ordenado ASC por (freq, vchannel)."""
        chs: list[Channel] = app.state.channels
        return ChannelsResponse(
            total=len(chs),
            channels=[_channel_to_view(i, c) for i, c in enumerate(chs)],
        )

    @app.get("/channels/{channel_id}")
    async def get_channel(channel_id: int) -> ChannelView:
        """Metadata completa de un canal (base del EPG en Stage 1)."""
        chs: list[Channel] = app.state.channels
        if not (0 <= channel_id < len(chs)):
            raise HTTPException(status_code=404, detail=f"channel_id {channel_id} fuera de rango")
        return _channel_to_view(channel_id, chs[channel_id])

    @app.post("/scan/{n}", status_code=202)
    async def start_scan(n: int) -> dict:
        """Arranca scan-channels.sh sobre adapter `n`. Tarda ~3 min y SOBREESCRIBE channels.conf.

        Rechaza si el adapter está reservado por el archiver (no podemos
        cambiarle el tune sin romper la grabación 24×7).
        """
        poller: MonitorPoller = app.state.poller
        if n not in poller.adapters:
            raise HTTPException(status_code=404, detail=f"adapter {n} no existe")
        if poller.is_reserved(n):
            raise HTTPException(
                status_code=409,
                detail=f"adapter {n} reservado por el archiver — DELETE /archive primero",
            )
        sm: ScanManager = app.state.scan
        session = await sm.start(n)
        return {
            "state": session.state,
            "adapter": n,
            "started_at": session.started_at,
            "log_path": str(session.log_path) if session.log_path else None,
        }

    @app.get("/scan/{n}")
    async def scan_status(n: int) -> dict:
        sm: ScanManager = app.state.scan
        s = sm.status(n)
        return {
            "adapter": s.adapter,
            "state": s.state,
            "progress": s.progress,
            "channels": s.channels_found,
            "started_at": s.started_at,
            "finished_at": s.finished_at,
            "error": s.error,
        }

    @app.post("/channels/reload", status_code=202)
    async def reload_channels() -> dict:
        """Re-lee channels.conf desde disco (después de un scan, p.ej.)."""
        app.state.channels = _load_catalog()
        return {"channels_count": len(app.state.channels)}

    @app.get("/multiplexes")
    async def list_multiplexes() -> MultiplexesResponse:
        """Catálogo agrupado por multiplex (frecuencia RF).

        Útil para entender qué tuner cubre qué subcanales:
        un tuner = un multiplex = todos los subcanales listados acá llegan en
        el mismo TS de `/stream/{n}.ts`.
        """
        chs: list[Channel] = app.state.channels
        muxes: dict[int, MultiplexView] = {}
        for i, c in enumerate(chs):
            mv = muxes.get(c.frequency_hz)
            if mv is None:
                mv = MultiplexView(
                    frequency_hz=c.frequency_hz,
                    delivery_system=c.delivery_system_name,
                    modulation=c.modulation_name,
                    channels=[],
                )
                muxes[c.frequency_hz] = mv
            mv.channels.append(_channel_to_view(i, c))
        items = sorted(muxes.values(), key=lambda m: m.frequency_hz)
        return MultiplexesResponse(total=len(items), multiplexes=items)

    @app.post("/tuners/{n}/tune", status_code=202)
    async def tune_tuner(n: int, body: TuneRequest) -> dict:
        """Sintoniza el adapter `n` al canal con `channel_id`.

        Respuesta inmediata (202 Accepted) — el lock real va a aparecer en el
        próximo batch del WS (típicamente 100-1500 ms después).
        """
        poller: MonitorPoller = app.state.poller
        chs: list[Channel] = app.state.channels
        if not (0 <= body.channel_id < len(chs)):
            raise HTTPException(status_code=404, detail=f"channel_id {body.channel_id} fuera de rango (0..{len(chs)-1})")
        if n not in poller.adapters:
            raise HTTPException(status_code=404, detail=f"adapter {n} no existe")
        if poller.is_reserved(n):
            raise HTTPException(
                status_code=409,
                detail=f"adapter {n} reservado por el archiver — DELETE /archive primero",
            )
        ch = chs[body.channel_id]
        try:
            poller.tune(n, ch.frequency_hz, ch.delivery_system, ch.modulation)
        except FrontendError as e:
            raise HTTPException(status_code=500, detail=f"tune failed: {e}") from e
        return {
            "status": "ok",
            "adapter": n,
            "channel_id": body.channel_id,
            "channel_name": ch.name,
            "frequency_hz": ch.frequency_hz,
        }

    @app.get("/stream/{n}.ts")
    async def stream_ts(n: int, request: Request) -> StreamingResponse:
        """Stream raw MPEG-TS del adapter `n` (todos los PIDs del multiplex actual).

        Requiere que el adapter esté previamente sintonizado (POST /tuners/{n}/tune).
        Si no hay tune, dvr0 no entrega datos y la conexión se cuelga hasta timeout.

        Content-type: `video/mp2t` — VLC, mpv, ffmpeg lo abren directo.
        """
        poller: MonitorPoller = app.state.poller
        if n not in poller.adapters:
            raise HTTPException(status_code=404, detail=f"adapter {n} no existe")

        # Verificar que está tuneado — si freq=0 nadie va a recibir nada
        snap = next((s for s in poller.current() if s.adapter == n), None)
        if snap is None or not snap.frequency_hz:
            raise HTTPException(
                status_code=409,
                detail=f"adapter {n} no está sintonizado — POST /tuners/{n}/tune primero",
            )

        loop = asyncio.get_running_loop()

        async def iter_ts() -> AsyncIterator[bytes]:
            dmx = Demux(adapter=n)
            dvr_fd: int | None = None
            try:
                dmx.open()
                dmx.set_filter_all_pids(buffer_kb=512)
                dvr_fd = os.open(dmx.dvr_path, os.O_RDONLY | os.O_NONBLOCK)
                log.info("stream/%d: opened, demux+dvr ready", n)

                while True:
                    if await request.is_disconnected():
                        log.info("stream/%d: client disconnected", n)
                        return
                    # Read en thread pool para no bloquear el loop
                    try:
                        chunk = await loop.run_in_executor(None, _read_with_retry, dvr_fd)
                    except OSError as e:
                        log.warning("stream/%d: read error: %s", n, e)
                        return
                    if not chunk:
                        # EOF teórico — no debería pasar con dvr0 mientras hay tune
                        await asyncio.sleep(0.05)
                        continue
                    yield chunk
            except (DemuxError, OSError) as e:
                log.exception("stream/%d: setup failed", n)
                # No podemos lanzar HTTPException acá (ya empezó el body) — solo terminar
                return
            finally:
                if dvr_fd is not None:
                    try:
                        os.close(dvr_fd)
                    except OSError:
                        pass
                dmx.close()
                log.info("stream/%d: closed", n)

        return StreamingResponse(
            iter_ts(),
            media_type="video/mp2t",
            headers={"Cache-Control": "no-store", "X-Accel-Buffering": "no"},
        )

    @app.get("/tuners/{n}/info")
    async def tuner_info(n: int, seconds: float = 3.0) -> dict:
        """Captura `seconds` (default 3s) del TS y devuelve todo lo extraíble:

        - **ffprobe**: programs, streams (codecs/resolución/bitrate/idioma/CC flag).
        - **closed_captions**: texto extraído por ccextractor (SRT crudo).
        - **bytes_captured**: bytes capturados.

        Usa `DMX_OUT_TSDEMUX_TAP`, no interfiere con `/stream/{n}.ts` activo.
        """
        import tempfile
        poller: MonitorPoller = app.state.poller
        if n not in poller.adapters:
            raise HTTPException(status_code=404, detail=f"adapter {n} no existe")
        snap = next((s for s in poller.current() if s.adapter == n), None)
        if snap is None or not snap.frequency_hz:
            raise HTTPException(
                status_code=409,
                detail=f"adapter {n} no está sintonizado — POST /tuners/{n}/tune primero",
            )
        if not (0.5 <= seconds <= 30):
            raise HTTPException(status_code=400, detail="seconds debe estar entre 0.5 y 30")

        with tempfile.NamedTemporaryFile(
            suffix=".ts", prefix=f"sint-adapter{n}-", delete=False
        ) as tf:
            ts_path = Path(tf.name)

        try:
            written = await capture_ts_seconds(adapter=n, seconds=seconds, out_path=ts_path)
            if written < 188 * 10:
                raise HTTPException(
                    status_code=502,
                    detail=f"captura demasiado corta ({written} bytes) — ¿señal floja o lock perdido?",
                )
            # Correr ffprobe + ccextractor en paralelo
            ffprobe_task = asyncio.create_task(run_ffprobe(ts_path))
            cc_task = asyncio.create_task(run_ccextractor(ts_path))
            probe, cc = await asyncio.gather(ffprobe_task, cc_task)
            return {
                "adapter": n,
                "frequency_hz": snap.frequency_hz,
                "captured_seconds": seconds,
                "bytes_captured": written,
                "ffprobe": probe,
                "closed_captions": cc,
            }
        finally:
            try:
                ts_path.unlink()
            except OSError:
                pass

    @app.delete("/tuners/{n}/tune", status_code=202)
    async def clear_tuner(n: int) -> dict:
        """Libera el tune del adapter (DTV_CLEAR)."""
        poller: MonitorPoller = app.state.poller
        if n not in poller.adapters:
            raise HTTPException(status_code=404, detail=f"adapter {n} no existe")
        if poller.is_reserved(n):
            raise HTTPException(
                status_code=409,
                detail=f"adapter {n} reservado por el archiver — DELETE /archive primero",
            )
        try:
            poller.clear(n)
        except FrontendError as e:
            raise HTTPException(status_code=500, detail=f"clear failed: {e}") from e
        return {"status": "ok", "adapter": n}

    @app.get("/archive")
    async def archive_status() -> dict:
        """Status del archiver: running, uptime, stats por pipeline."""
        archiver: Archiver = app.state.archiver
        return archiver.status()

    @app.post("/archive", status_code=202)
    async def archive_start() -> dict:
        """Arranca el archiver (idempotente si ya está running)."""
        archiver: Archiver = app.state.archiver
        if archiver.is_running:
            return {"status": "already-running", "pipelines": len(archiver.pipelines)}
        try:
            await archiver.start()
        except Exception as e:
            raise HTTPException(status_code=500, detail=f"start failed: {e}") from e
        return {
            "status": "started",
            "pipelines": len(archiver.pipelines),
            "reserved_adapters": sorted(archiver.reserved_adapters),
        }

    @app.delete("/archive", status_code=202)
    async def archive_stop() -> dict:
        """Para el archiver, libera los tuners reservados."""
        archiver: Archiver = app.state.archiver
        if not archiver.is_running:
            return {"status": "already-stopped"}
        await archiver.stop()
        return {"status": "stopped"}

    @app.websocket("/ws/archive/{slug}")
    async def ws_archive_tail(ws: WebSocket, slug: str) -> None:
        """Live view del archiver para un subcanal `slug` (ej. '2.1-XHGA').

        Al conectar:
        - manda hasta 20 líneas del .txt actual del período (catch-up)
        - subscribe al pipeline para events {type:"cc", text, wall_clock, ...}
        - desuscribe al cerrar
        """
        await ws.accept()
        archiver: Archiver = app.state.archiver
        if not archiver.is_running:
            await ws.send_text(json.dumps({"type": "error", "message": "archiver no está corriendo"}))
            await ws.close()
            return
        pipeline = next((p for p in archiver.pipelines if p.target.slug == slug), None)
        if pipeline is None:
            await ws.send_text(json.dumps({
                "type": "error",
                "message": f"slug {slug!r} no existe — slugs válidos: " +
                           ", ".join(sorted(p.target.slug for p in archiver.pipelines)),
            }))
            await ws.close()
            return

        # Tail inicial: últimas N líneas del .txt actual
        txt_path = pipeline.stats.current_txt_path
        if txt_path:
            try:
                from collections import deque
                with open(txt_path, "r", encoding="utf-8", errors="replace") as f:
                    tail = list(deque(f, maxlen=20))
                for line in tail:
                    line = line.rstrip("\n")
                    if line:
                        await ws.send_text(json.dumps({"type": "tail", "line": line}))
                await ws.send_text(json.dumps({"type": "tail_end"}))
            except (OSError, ValueError) as e:
                log.warning("ws_archive tail %s: %s", slug, e)

        # Subscribe a eventos en vivo
        queue = pipeline.subscribe()
        log.info("ws_archive %s: cliente conectado", slug)
        try:
            while True:
                event = await queue.get()
                await ws.send_text(json.dumps(event))
        except WebSocketDisconnect:
            log.debug("ws_archive %s: cliente desconectó", slug)
        except Exception:
            log.exception("ws_archive %s: stream error", slug)
        finally:
            pipeline.unsubscribe(queue)

    @app.websocket("/ws/cc/{n}")
    async def ws_live_cc(ws: WebSocket, n: int) -> None:
        """Live Closed Captions del adapter `n`.

        Cliente conecta → se suscribe a la sesión Live CC (la inicia si nadie
        más estaba conectado). Eventos:
          - {"type": "status", "state": "running"|"stopped"}
          - {"type": "cc", "seq": N, "start_ms": ..., "end_ms": ..., "text": "…"}
          - {"type": "error", "message": "…"}
        """
        await ws.accept()
        poller: MonitorPoller = app.state.poller
        if n not in poller.adapters:
            await ws.send_text(json.dumps({"type": "error", "message": f"adapter {n} no existe"}))
            await ws.close()
            return

        # Verificar que esté tuneado — sino ccextractor no recibe TS y se queda inerte
        snap = next((s for s in poller.current() if s.adapter == n), None)
        if snap is None or not snap.frequency_hz:
            await ws.send_text(json.dumps({
                "type": "error",
                "message": f"adapter {n} no está sintonizado",
            }))
            await ws.close()
            return

        mgr: LiveCCManager = app.state.live_cc
        session = mgr.get(n)
        queue = await session.subscribe()
        log.info("ws_cc adapter %d: cliente conectado (subscribers=%d)", n, len(session.subscribers))
        try:
            while True:
                event = await queue.get()
                await ws.send_text(json.dumps(event))
        except WebSocketDisconnect:
            log.debug("ws_cc adapter %d: cliente desconectó", n)
        except Exception:
            log.exception("ws_cc adapter %d: stream error", n)
        finally:
            await session.unsubscribe(queue)

    @app.websocket("/ws/stats")
    async def ws_stats(ws: WebSocket) -> None:
        await ws.accept()
        poller: MonitorPoller = app.state.poller

        # Mandar snapshot inicial inmediato para no hacer esperar al cliente
        try:
            initial = poller.current()
            if initial:
                msg = StatsMessage(timestamp=time.time(), tuners=[_snap_to_view(s) for s in initial])
                await ws.send_text(msg.model_dump_json())
        except Exception:
            log.exception("error sending initial snapshot")

        # Loop principal: tirar cada batch del poller al cliente
        try:
            async for batch in poller.subscribe():
                msg = StatsMessage(
                    timestamp=time.time(),
                    tuners=[_snap_to_view(s) for s in batch],
                )
                await ws.send_text(msg.model_dump_json())
        except WebSocketDisconnect:
            log.debug("ws client disconnected")
        except Exception:
            log.exception("ws stream error")
            try:
                await ws.close()
            except Exception:
                pass

    return app


# Para uvicorn: `uvicorn sintonizador.api.app:app`
app = create_app()
