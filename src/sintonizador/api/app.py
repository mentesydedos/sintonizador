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
import re
import time
from contextlib import asynccontextmanager
from dataclasses import asdict
from pathlib import Path
from typing import AsyncIterator

from fastapi import FastAPI, HTTPException, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, HTMLResponse, StreamingResponse

from sintonizador.api.models import (
    ChannelsResponse,
    ChannelView,
    MonitorRequest,
    MultiplexesResponse,
    MultiplexView,
    StatsMessage,
    SubchannelStatusView,
    TuneRequest,
    TunerView,
)
from sintonizador.archiver import Archiver
from sintonizador.archiver.config import make_slug
from sintonizador.monitor.cc_consumer import SubchannelCCManager
from sintonizador.monitor.monitor_service import MonitorService
from sintonizador.monitor.program_stream import ProgramTsConsumer
from sintonizador.monitor.subchannel_status import probe_subchannel
from sintonizador.monitor.transcode import TranscodeManager
from sintonizador.channels import Channel, channel_sort_key, parse_file
from sintonizador.scan import ScanManager
from sintonizador.dvb import detect_adapters
from sintonizador.dvb.demux import Demux, DemuxError
from sintonizador.dvb.frontend import FrontendError
from sintonizador.extract import LiveCCManager, capture_ts_seconds, run_ccextractor, run_ffprobe
from sintonizador.monitor import MonitorPoller, TunerSnapshot
from sintonizador.mux import MuxReaderRegistry
from sintonizador.videorec import VideoRecorder
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

# --- Grabación de video 24x7 (H.264/AAC, segmentos de 30 min) ---
# Ruta configurable — en producción apunta a un mount de NAS compartido con
# transcriber-linux (alerts/clips.py lee de ahí para el snapshot/clip del
# dashboard AlertaTV, sin ningún cambio de código en ese lado).
VIDEOREC_ROOT = Path(os.environ.get("SINTONIZADOR_VIDEOREC_ROOT", "/home/sintonizador/video"))
AUTOSTART_VIDEOREC = os.environ.get("SINTONIZADOR_VIDEOREC_AUTOSTART", "true").lower() not in ("0", "false", "no")
# nvenc (default, requiere GPU NVIDIA con NVENC) | software (libx264, CPU).
VIDEOREC_ENCODER = os.environ.get("SINTONIZADOR_VIDEOREC_ENCODER", "nvenc").lower()
# Tope de pipelines simultáneas en NVENC antes de caer a libx264 — el techo
# real de la tarjeta (T1000 u otra) es desconocido hasta probarlo; default
# conservador. Ver plan de verificación (subir de a 1 monitoreando nvidia-smi).
VIDEOREC_NVENC_LIMIT = int(os.environ.get("SINTONIZADOR_VIDEOREC_NVENC_LIMIT", "4"))
VIDEOREC_BITRATE = os.environ.get("SINTONIZADOR_VIDEOREC_BITRATE", "5M")
VIDEOREC_MAXRATE = os.environ.get("SINTONIZADOR_VIDEOREC_MAXRATE", "6M")
VIDEOREC_BUFSIZE = os.environ.get("SINTONIZADOR_VIDEOREC_BUFSIZE", "10M")
VIDEOREC_ROTATION_MIN = int(os.environ.get("SINTONIZADOR_VIDEOREC_ROTATION_MINUTES", "30"))

# --- Video preview (HLS) ---
HLS_ROOT = Path(os.environ.get("SINTONIZADOR_HLS_ROOT", "/tmp/sintonizador-hls"))
# Encoder: software (libx264, default y fiable) | vaapi | qsv. El iGPU de la Z2
# no transcodifica fiable hoy → software por defecto (ver memoria).
HLS_ENCODER = os.environ.get("SINTONIZADOR_HLS_ENCODER", "software").lower()
# Tope de transcodes simultáneos (el iGPU/CPU no da para 16).
HLS_MAX_CONCURRENT = int(os.environ.get("SINTONIZADOR_HLS_MAX", "5"))
_SLUG_OK = re.compile(r"^[A-Za-z0-9._-]+$")
_SEG_OK = re.compile(r"^(playlist\.m3u8|seg_\d+\.ts)$")


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
    adapters = detect_adapters()
    app.state.adapters = adapters
    poller = MonitorPoller(adapters=adapters, interval_s=0.5)
    await poller.start()
    app.state.poller = poller
    app.state.channels = _load_catalog()
    app.state.live_cc = LiveCCManager()
    # Registry de MuxReaders: un tap por adapter, compartido por archiver +
    # monitoreo interactivo (CC live + HLS) + capturas de estado.
    app.state.mux_readers = MuxReaderRegistry()
    app.state.subch_cc = SubchannelCCManager(app.state.mux_readers)
    app.state.transcode = TranscodeManager(
        registry=app.state.mux_readers,
        hls_root=HLS_ROOT,
        max_concurrent=HLS_MAX_CONCURRENT,
        encoder=HLS_ENCODER,
    )
    await app.state.transcode.start()
    app.state.monitor = MonitorService(poller=poller, channels=app.state.channels)
    app.state.scan = ScanManager(
        script_path=Path("/home/sintonizador/scripts/scan-channels.sh"),
        channels_conf_path=DEFAULT_CHANNELS_PATH,
    )
    app.state.archiver = Archiver(
        poller=poller,
        channels=app.state.channels,
        archive_root=DEFAULT_ARCHIVE_ROOT,
        registry=app.state.mux_readers,
        rotation_minutes=DEFAULT_ROTATION_MIN,
    )
    if AUTOSTART_ARCHIVER:
        try:
            await app.state.archiver.start()
        except Exception:
            log.exception("archiver: auto-start falló — continuando sin archiver")
    # VideoRecorder depende de que el archiver ya haya tuneado/reservado los
    # adapters — por eso se instancia y arranca DESPUÉS de app.state.archiver.start().
    app.state.videorec = VideoRecorder(
        archiver=app.state.archiver,
        registry=app.state.mux_readers,
        output_root=VIDEOREC_ROOT,
        encoder=VIDEOREC_ENCODER,
        nvenc_limit=VIDEOREC_NVENC_LIMIT,
        bitrate=VIDEOREC_BITRATE,
        maxrate=VIDEOREC_MAXRATE,
        bufsize=VIDEOREC_BUFSIZE,
        rotation_minutes=VIDEOREC_ROTATION_MIN,
    )
    if AUTOSTART_VIDEOREC:
        try:
            await app.state.videorec.start()
        except Exception:
            log.exception("videorec: auto-start falló — continuando sin grabación de video")
    try:
        yield
    finally:
        # videorec se para ANTES que el archiver (orden simétrico al arranque):
        # depende de que el archiver siga con los adapters reservados mientras corre.
        try:
            await app.state.videorec.stop()
        except Exception:
            log.exception("videorec: stop falló durante shutdown")
        try:
            await app.state.archiver.stop()
        except Exception:
            log.exception("archiver: stop falló durante shutdown")
        monitor = getattr(app.state, "monitor", None)
        if monitor is not None:
            try:
                await monitor.teardown()
            except Exception:
                log.exception("monitor: teardown falló durante shutdown")
        try:
            await app.state.transcode.teardown()
        except Exception:
            log.exception("transcode: teardown falló durante shutdown")
        try:
            await app.state.subch_cc.teardown()
        except Exception:
            log.exception("subch_cc: teardown falló durante shutdown")
        await app.state.live_cc.shutdown()
        await app.state.mux_readers.stop_all()
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

    @app.get("/hls.min.js")
    async def hls_js() -> FileResponse:
        """hls.js vendorizado para el preview de video en navegadores no-Safari."""
        return FileResponse(
            INDEX_PATH.parent / "hls.min.js",
            media_type="application/javascript",
            headers={"Cache-Control": "max-age=86400"},
        )

    @app.get("/adapters")
    async def list_adapters() -> dict:
        """Topología estática: qué adaptadores existen. El frontend arma N tarjetas.

        Disponible aún antes del primer batch del poller (a diferencia de /tuners).
        """
        adapters: list[int] = app.state.adapters
        return {"adapters": adapters, "count": len(adapters)}

    @app.get("/mux")
    async def mux_debug() -> dict:
        """Debug: consumidores y bytes por MuxReader. Verifica un-tap-por-adapter."""
        registry: MuxReaderRegistry = app.state.mux_readers
        return {"readers": registry.snapshot()}

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
        if app.state.monitor.owns(n):
            raise HTTPException(
                status_code=409,
                detail=f"adapter {n} en uso por el monitoreo — DELETE /monitor primero",
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
        app.state.monitor.refresh_channels(app.state.channels)
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

    @app.get("/subchannels/{channel_id}/status")
    async def subchannel_status(channel_id: int, seconds: float = 3.0) -> SubchannelStatusView:
        """Estado de un subcanal: señal del mux (del poller) + codec/idioma/CC
        del programa (probe via el MuxReader compartido, sin segundo tap)."""
        chs: list[Channel] = app.state.channels
        if not (0 <= channel_id < len(chs)):
            raise HTTPException(status_code=404, detail=f"channel_id {channel_id} fuera de rango")
        ch = chs[channel_id]
        poller: MonitorPoller = app.state.poller
        registry: MuxReaderRegistry = app.state.mux_readers
        slug = make_slug(ch.vchannel, ch.name)
        view = SubchannelStatusView(
            slug=slug, channel_id=channel_id, name=ch.name, vchannel=ch.vchannel,
            frequency_hz=ch.frequency_hz, service_id=ch.service_id,
        )
        snap = next((s for s in poller.current() if s.frequency_hz == ch.frequency_hz), None)
        if snap is None:
            view.error = "mux no sintonizado en ningún tuner"
            return view
        view.adapter = snap.adapter
        view.on_air = snap.has_lock
        view.has_signal = snap.has_signal
        view.signal_dbm = snap.signal_dbm
        view.cnr_db = snap.cnr_db
        if not snap.has_lock:
            view.error = "mux sintonizado pero sin lock todavía"
            return view
        try:
            probe = await probe_subchannel(registry, snap.adapter, ch.service_id, seconds=seconds)
        except Exception as e:
            log.exception("probe subchannel %s falló", slug)
            view.error = f"probe falló: {e}"
            return view
        view.video_codec = probe.get("video_codec")
        view.width = probe.get("width")
        view.height = probe.get("height")
        view.audio_codec = probe.get("audio_codec")
        view.audio_language = probe.get("audio_language")
        view.cc_present = bool(probe.get("cc_present"))
        view.bytes_captured = probe.get("bytes_captured", 0)
        if probe.get("error"):
            view.error = probe["error"]
        # Augment cc_present: si el archiver o un LiveCCConsumer ya vieron CC
        # reciente para este slug (la captura puntual pudo caer en un hueco).
        if not view.cc_present:
            now = time.time()
            archiver: Archiver = app.state.archiver
            pipe = next((p for p in archiver.pipelines if p.target.slug == slug), None)
            if pipe and pipe.stats.last_event_time and (now - pipe.stats.last_event_time) < 20:
                view.cc_present = True
            live = app.state.subch_cc.get(slug)
            if live and live.last_cc_at and (now - live.last_cc_at) < 20:
                view.cc_present = True
        return view

    @app.post("/subchannels/{channel_id}/video", status_code=202)
    async def start_subchannel_video(channel_id: int) -> dict:
        """Arranca (o reusa) el transcode HLS del subcanal. Respeta el tope global."""
        chs: list[Channel] = app.state.channels
        if not (0 <= channel_id < len(chs)):
            raise HTTPException(status_code=404, detail=f"channel_id {channel_id} fuera de rango")
        ch = chs[channel_id]
        poller: MonitorPoller = app.state.poller
        snap = next((s for s in poller.current() if s.frequency_hz == ch.frequency_hz), None)
        if snap is None or not snap.has_lock:
            raise HTTPException(
                status_code=409,
                detail=f"mux de {ch.vchannel} no sintonizado/sin lock — monitoreá su frecuencia primero",
            )
        mgr: TranscodeManager = app.state.transcode
        try:
            result = await mgr.start_transcode(ch, snap.adapter)
        except ValueError as e:
            raise HTTPException(status_code=400, detail=str(e)) from e
        except RuntimeError as e:
            raise HTTPException(status_code=429, detail=str(e)) from e
        return result

    @app.delete("/subchannels/{channel_id}/video", status_code=202)
    async def stop_subchannel_video(channel_id: int) -> dict:
        chs: list[Channel] = app.state.channels
        if not (0 <= channel_id < len(chs)):
            raise HTTPException(status_code=404, detail=f"channel_id {channel_id} fuera de rango")
        ch = chs[channel_id]
        slug = make_slug(ch.vchannel, ch.name)
        stopped = await app.state.transcode.stop_transcode(slug)
        return {"status": "stopped" if stopped else "not-running", "slug": slug}

    @app.get("/hls/{slug}/{filename}")
    async def hls_file(slug: str, filename: str):
        """Sirve playlist y segmentos HLS del transcode `slug`."""
        if not _SLUG_OK.match(slug) or not _SEG_OK.match(filename):
            raise HTTPException(status_code=400, detail="ruta HLS inválida")
        mgr: TranscodeManager = app.state.transcode
        if filename == "playlist.m3u8":
            # Mantener vivo el transcode mientras el browser pide el playlist.
            if not mgr.touch(slug):
                raise HTTPException(status_code=404, detail=f"transcode {slug} no activo")
        path = (HLS_ROOT / slug / filename).resolve()
        # Defensa extra anti-traversal: el path resuelto debe estar bajo HLS_ROOT/slug
        if not str(path).startswith(str((HLS_ROOT / slug).resolve())):
            raise HTTPException(status_code=400, detail="ruta inválida")
        if not path.exists():
            raise HTTPException(status_code=404, detail="aún no disponible (arrancando transcode)")
        media_type = "application/vnd.apple.mpegurl" if filename.endswith(".m3u8") else "video/mp2t"
        headers = {"Cache-Control": "no-store"} if filename.endswith(".m3u8") else {"Cache-Control": "max-age=10"}
        return FileResponse(path, media_type=media_type, headers=headers)

    @app.get("/monitor")
    async def get_monitor() -> dict:
        """Selección actual de subcanales monitoreados + plan (mux→tuner)."""
        monitor: MonitorService = app.state.monitor
        return monitor.status()

    @app.post("/monitor")
    async def post_monitor(body: MonitorRequest) -> dict:
        """Define la selección de subcanales: calcula muxes, asigna tuners y sintoniza.

        400 si pide más muxes que tuners disponibles o un channel_id inválido.
        """
        monitor: MonitorService = app.state.monitor
        try:
            return monitor.set_selection(body.channel_ids)
        except ValueError as e:
            raise HTTPException(status_code=400, detail=str(e)) from e
        except FrontendError as e:
            raise HTTPException(status_code=500, detail=f"tune falló: {e}") from e

    @app.get("/monitor/plan")
    async def monitor_plan(channel_ids: str) -> dict:
        """Dry-run: calcula el plan para una lista de ids (CSV) sin tunear."""
        monitor: MonitorService = app.state.monitor
        try:
            ids = [int(x) for x in channel_ids.split(",") if x.strip() != ""]
        except ValueError as e:
            raise HTTPException(status_code=400, detail=f"channel_ids inválido: {e}") from e
        try:
            return monitor.plan(ids)
        except ValueError as e:
            raise HTTPException(status_code=400, detail=str(e)) from e

    @app.delete("/monitor", status_code=202)
    async def delete_monitor() -> dict:
        """Para el monitoreo: libera los tuners que tomó (deja el archiver intacto)."""
        monitor: MonitorService = app.state.monitor
        # Parar videos activos antes de soltar los tuners
        transcode: TranscodeManager = app.state.transcode
        for slug in transcode.active_slugs:
            await transcode.stop_transcode(slug)
        await monitor.teardown()
        return {"status": "stopped"}

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
        if app.state.monitor.owns(n):
            raise HTTPException(
                status_code=409,
                detail=f"adapter {n} en uso por el monitoreo — DELETE /monitor primero",
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

    @app.get("/stream/sub/{channel_id}.ts")
    async def stream_subchannel(channel_id: int, request: Request) -> StreamingResponse:
        """Stream MPEG-TS de UN subcanal (programa) — video (CC) + audio.

        Filtra el programa del mux vía el fan-out compartido (un solo tap). Pensado
        para que el transcriber externo (ccextractor + Qwen3-ASR) consuma el subcanal
        local como reemplazo del Tvheadend. Requiere el mux tuneado (archiver/monitor).
        """
        chs: list[Channel] = app.state.channels
        if not (0 <= channel_id < len(chs)):
            raise HTTPException(status_code=404, detail=f"channel_id {channel_id} fuera de rango")
        ch = chs[channel_id]
        if ch.service_id is None:
            raise HTTPException(status_code=400, detail="canal sin service_id — no se puede filtrar el programa")
        poller: MonitorPoller = app.state.poller
        # Gate por mux SINTONIZADO (freq), no por has_lock: el bit de lock del
        # lgdt3306a se pone errático bajo carga del archiver aunque el TS fluya.
        snap = next((s for s in poller.current()
                     if s.frequency_hz == ch.frequency_hz), None)
        if snap is None:
            raise HTTPException(
                status_code=409,
                detail=f"mux de {ch.vchannel} no sintonizado — el archiver o /monitor deben tenerlo activo",
            )
        adapter = snap.adapter
        registry: MuxReaderRegistry = app.state.mux_readers
        app.state._progts_seq = getattr(app.state, "_progts_seq", 0) + 1
        key = f"progts:{channel_id}:{app.state._progts_seq}"
        consumer = ProgramTsConsumer(key=key, service_id=ch.service_id)
        await registry.attach(adapter, consumer)
        log.info("stream/sub %s: cliente conectado (adapter %d)", make_slug(ch.vchannel, ch.name), adapter)

        async def gen() -> AsyncIterator[bytes]:
            try:
                while True:
                    if await request.is_disconnected():
                        break
                    try:
                        chunk = await asyncio.wait_for(consumer.read(), timeout=5.0)
                    except asyncio.TimeoutError:
                        continue
                    if chunk:
                        yield chunk
            finally:
                await registry.detach(adapter, key)
                log.info("stream/sub %s: cliente desconectado", make_slug(ch.vchannel, ch.name))

        return StreamingResponse(
            gen(), media_type="video/mp2t",
            headers={"Cache-Control": "no-store", "X-Accel-Buffering": "no"},
        )

    @app.get("/playlist/transcriber.m3u")
    async def transcriber_playlist(request: Request) -> StreamingResponse:
        """m3u de los subcanales de los muxes tuneados → /stream/sub/{id}.ts.

        El transcriber-linux lo usa como fuente local (reemplazo del Tvheadend).
        Incluye solo subcanales cuyo mux está sintonizado con lock ahora mismo.
        """
        chs: list[Channel] = app.state.channels
        poller: MonitorPoller = app.state.poller
        # Muxes sintonizados (freq seteada); no filtramos por has_lock (errático bajo carga).
        tuned = {s.frequency_hz for s in poller.current() if s.frequency_hz}
        host = request.headers.get("host", "127.0.0.1:8000")
        scheme = request.url.scheme
        lines = ["#EXTM3U"]
        for i, c in enumerate(chs):
            if c.frequency_hz not in tuned or c.service_id is None:
                continue
            name = f"{c.vchannel or '?'} {c.name}"
            lines.append(f'#EXTINF:-1 tvg-chno="{c.vchannel or ""}",{name}')
            lines.append(f"{scheme}://{host}/stream/sub/{i}.ts")
        body = "\n".join(lines) + "\n"
        return StreamingResponse(iter([body.encode()]), media_type="audio/x-mpegurl")

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
        if app.state.monitor.owns(n):
            raise HTTPException(
                status_code=409,
                detail=f"adapter {n} en uso por el monitoreo — DELETE /monitor primero",
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

    @app.get("/videorec")
    async def videorec_status() -> dict:
        """Status de la grabación de video: running, uptime, stats por pipeline."""
        videorec: VideoRecorder = app.state.videorec
        return videorec.status()

    @app.post("/videorec", status_code=202)
    async def videorec_start() -> dict:
        """Arranca la grabación de video (idempotente si ya está running).

        Requiere que el archiver de CC esté corriendo (los adapters deben
        estar ya tuneados/reservados) — 409 si no.
        """
        videorec: VideoRecorder = app.state.videorec
        if videorec.is_running:
            return {"status": "already-running", "pipelines": len(videorec.pipelines)}
        archiver: Archiver = app.state.archiver
        if not archiver.is_running:
            raise HTTPException(
                status_code=409,
                detail="el archiver de CC no está corriendo — POST /archive primero",
            )
        try:
            await videorec.start()
        except Exception as e:
            raise HTTPException(status_code=500, detail=f"start failed: {e}") from e
        return {"status": "started", "pipelines": len(videorec.pipelines)}

    @app.delete("/videorec", status_code=202)
    async def videorec_stop() -> dict:
        """Para la grabación de video (deja el archiver de CC intacto)."""
        videorec: VideoRecorder = app.state.videorec
        if not videorec.is_running:
            return {"status": "already-stopped"}
        await videorec.stop()
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

    @app.websocket("/ws/subchannel/cc/{channel_id}")
    async def ws_subchannel_cc(ws: WebSocket, channel_id: int) -> None:
        """CC en vivo de UN subcanal (programa) por su channel_id del catálogo.

        Si el archiver 24×7 ya cubre ese subcanal, reusa su pipeline (mismo -pn);
        si no, crea un LiveCCConsumer on-demand colgado del MuxReader compartido.
        """
        await ws.accept()
        chs: list[Channel] = app.state.channels
        if not (0 <= channel_id < len(chs)):
            await ws.send_text(json.dumps({"type": "error", "message": "channel_id fuera de rango"}))
            await ws.close(); return
        ch = chs[channel_id]
        if ch.service_id is None:
            await ws.send_text(json.dumps({"type": "error", "message": "canal sin service_id — no se puede filtrar CC"}))
            await ws.close(); return
        slug = make_slug(ch.vchannel, ch.name)
        poller: MonitorPoller = app.state.poller
        snap = next((s for s in poller.current() if s.frequency_hz == ch.frequency_hz), None)
        if snap is None or not snap.has_lock:
            await ws.send_text(json.dumps({
                "type": "error",
                "message": f"mux de {slug} no sintonizado/sin lock — monitoreá o sintonizá su frecuencia primero",
            }))
            await ws.close(); return
        adapter = snap.adapter

        # Reuso del archiver si ya tiene este subcanal
        archiver: Archiver = app.state.archiver
        pipe = None
        if archiver.is_running:
            pipe = next((p for p in archiver.pipelines if p.target.slug == slug), None)

        if pipe is not None:
            queue = pipe.subscribe()
            await ws.send_text(json.dumps({"type": "status", "state": "running", "source": "archiver", "slug": slug}))
            log.info("ws_subchannel_cc %s: vía archiver", slug)
            try:
                while True:
                    await ws.send_text(json.dumps(await queue.get()))
            except WebSocketDisconnect:
                pass
            except Exception:
                log.exception("ws_subchannel_cc %s (archiver): stream error", slug)
            finally:
                pipe.unsubscribe(queue)
            return

        # On-demand
        mgr: SubchannelCCManager = app.state.subch_cc
        _consumer, queue = await mgr.subscribe(slug, adapter, ch.service_id)
        await ws.send_text(json.dumps({"type": "status", "state": "running", "source": "live", "slug": slug}))
        log.info("ws_subchannel_cc %s: vía LiveCCConsumer (adapter %d, program %d)", slug, adapter, ch.service_id)
        try:
            while True:
                await ws.send_text(json.dumps(await queue.get()))
        except WebSocketDisconnect:
            pass
        except Exception:
            log.exception("ws_subchannel_cc %s (live): stream error", slug)
        finally:
            await mgr.unsubscribe(slug, queue)

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
