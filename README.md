# sintonizador

Software de control y monitoreo para tarjeta sintonizadora **TBS6704** (Quad ATSC/Clear-QAM PCIe) bajo Linux.

## Estado actual

- [x] Hardware detectado (TBS6704, PCI `544d:6178`, subsystem `6704:0001`)
- [x] Driver `tbsecp3` compilado e instalado contra kernel `6.8.0-107-generic`
- [x] `/dev/dvb/adapter[0-3]` operativos (4 frontends ATSC/QAMB registrados)
- [x] Scan ATSC inicial (36 canales en `channels.conf`)
- [x] Wrapper Python sobre DVB API v5 (`sintonizador.dvb.Frontend`)
- [x] FastAPI + WebSocket para los 4 tuners (`/tuners`, `/ws/stats`)
- [x] Web UI dashboard (tarjetas de tuner en vivo + monitoreo de subcanales)
- [x] Detección dinámica de adaptadores (escala 4→8 al añadir la 2da TBS6704)
- [x] Monitoreo de hasta 16 subcanales: estado/señal + Closed Captions en vivo + video preview (HLS)
- [x] Streaming HTTP MPEG-TS
- [x] Archiver 24×7 de Closed Captions
- [x] Grabación programada (video 24×7, H.264/AAC vía NVENC con fallback CPU)
- [ ] Pipeline de transcripción (Whisper)

## Capas

```
┌─────────────────────────────────────────────────────────┐
│  Web UI (HTMX + JS)  ←──  WebSocket /ws/stats          │
├─────────────────────────────────────────────────────────┤
│  FastAPI:  /tuners /channels /scan /record /stream     │
├─────────────────────────────────────────────────────────┤
│  Servicios:  scheduler · streamer · transcriber        │
├─────────────────────────────────────────────────────────┤
│  dvb/  wrapper Python sobre ioctl FE_* y dvr0          │
├─────────────────────────────────────────────────────────┤
│  Kernel:  tbsecp3 + lgdt3306a (ATSC demod)             │
├─────────────────────────────────────────────────────────┤
│  Hardware:  TBS6704 PCIe — 4 tuners ATSC/QAM           │
└─────────────────────────────────────────────────────────┘
```

## Setup

Ver [INSTALL.md](INSTALL.md) para la instalación paso a paso en una máquina nueva.
Ver [HISTORICO.md](HISTORICO.md) para el detalle de qué se probó, qué falló y por qué.

Quick path (asume Ubuntu 24.04 + kernel ≤6.14):

```bash
sudo bash scripts/build-tbs-clean.sh   # build + install + modprobe tbsecp3
newgrp video                           # para acceso sin sudo a /dev/dvb/*
bash scripts/verify-driver.sh
```

> ⚠️ NO usar `install-tbs-driver.sh` ni `resume-build.sh` directamente — ver HISTORICO.md, ambos
> tienen problemas conocidos en kernel 6.8. `build-tbs-clean.sh` es el script que funciona.

## Monitor en vivo (MVP)

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -e ".[dev]"
python -m sintonizador.main
# luego en otra terminal:
curl http://127.0.0.1:8000/tuners
# o conectarse al WebSocket /ws/stats que pushea cada 500ms
```

Endpoints actuales:

| Endpoint | Descripción |
|---|---|
| `GET /`                          | Dashboard web (4 tuner cards en vivo vía WS, con tune control) |
| `GET /tuners`                    | Snapshot HTTP de los 4 tuners |
| `WS  /ws/stats`                  | Push continuo de stats (cada 500ms) |
| `GET /channels`                  | Catálogo de canales (parsea `channels.conf` al iniciar) |
| `POST /tuners/{n}/tune`          | Sintoniza adapter `n` a un `channel_id` |
| `DELETE /tuners/{n}/tune`        | Libera el tune del adapter (DTV_CLEAR) |
| `GET /stream/{n}.ts`             | Stream MPEG-TS raw del adapter (requiere tune previo). `Content-Type: video/mp2t` |
| `GET /tuners/{n}/info?seconds=N` | Captura N seg, corre ffprobe + ccextractor, devuelve JSON con programs/streams + texto de Closed Captions |
| `WS  /ws/cc/{n}`                 | Live Closed Captions: pipeline TS → ccextractor en pipe → eventos JSON `{type:"cc", seq, start_ms, end_ms, text}` |
| `GET /archive`                   | Status del archiver 24×7 (pipelines, blocks_written, restarts, último evento por subcanal) |
| `POST /archive`                  | Arranca el archiver — tunea los 4 mux + lanza 9 pipelines + reserva los tuners |
| `DELETE /archive`                | Para el archiver, libera los tuners |
| `WS  /ws/archive/{slug}`         | Live view per-subcanal: catch-up con las últimas líneas del .txt actual + stream continuo de eventos `cc` (slug = '2.1-XHGA', '3.1-XHCTGD', etc.) |
| `GET /videorec`                  | Status de la grabación de video 24×7 (pipelines, encoder por canal, bytes, restarts) |
| `POST /videorec`                 | Arranca la grabación de video — requiere que el archiver ya esté corriendo (409 si no) |
| `DELETE /videorec`               | Para la grabación de video, deja el archiver de CC intacto |
| `GET /adapters`                  | Topología: `{adapters:[...], count:N}` — escala dinámica 4→8 |
| `GET /mux`                       | Debug: consumidores y bytes por MuxReader (verifica un-tap-por-adapter) |
| `GET /subchannels/{id}/status`   | Estado de un subcanal: señal del mux + codec/resolución/idioma/CC del programa |
| `WS  /ws/subchannel/cc/{id}`     | CC en vivo de UN subcanal (`-pn service_id`); reusa el archiver si ya lo cubre |
| `POST /subchannels/{id}/video`   | Arranca transcode HLS del subcanal → `{playlist}`. Tope global de simultáneos (429) |
| `DELETE /subchannels/{id}/video` | Para el transcode |
| `GET /hls/{slug}/{file}`         | Sirve playlist.m3u8 + segmentos HLS del transcode |
| `POST /monitor`                  | Define la selección (hasta 16 subcanales): calcula muxes, asigna tuners y sintoniza. 400 si faltan tuners |
| `GET /monitor`                   | Selección + plan (mux→tuner) actuales |
| `GET /monitor/plan?channel_ids=` | Dry-run del plan sin tunear |
| `DELETE /monitor`                | Para el monitoreo, libera sus tuners (deja el archiver intacto) |

### Arquitectura del fan-out compartido (`mux/`)

Un solo **tap** (demux fd) por adapter: `MuxReader` lee el mux una vez y hace fan-out
en user-space a N **consumidores** (`TsConsumer`): pipelines del archiver, CC en vivo,
transcode HLS, capturas de estado. Esto respeta el constraint del kernel (más de un tap
por adapter duplica paquetes y satura el buffer). El `MuxReaderRegistry` garantiza el
un-tap-por-adapter; archiver y monitoreo comparten el mismo reader cuando coinciden en mux.

Variables de entorno:
- `SINTONIZADOR_CHANNELS` — path al `channels.conf`. Default: `/home/sintonizador/channels.conf`.
- `SINTONIZADOR_ARCHIVE` — directorio raíz del archive 24×7. Default: `/home/sintonizador/archive`.
- `SINTONIZADOR_ROTATION_MINUTES` — minutos por archivo. Default: `30`.
- `SINTONIZADOR_ARCHIVE_AUTOSTART` — `true` (default): el archiver arranca con uvicorn. `false`: arranque vía POST /archive.
- `SINTONIZADOR_ADAPTERS` — override CSV de adaptadores (p.ej. `0,1,2,3,4,5,6,7` para probar 8 sin hardware). Default: auto-detección por `/dev/dvb/adapter*`.
- `SINTONIZADOR_HLS_ROOT` — dir temporal de segmentos HLS. Default: `/tmp/sintonizador-hls`.
- `SINTONIZADOR_HLS_ENCODER` — `software` (default, libx264) | `vaapi` | `qsv`. El iGPU no transcodifica fiable hoy (ver notas); software va bien hasta el tope.
- `SINTONIZADOR_HLS_MAX` — tope de transcodes de video simultáneos. Default: `5`.
- `SINTONIZADOR_VIDEOREC_ROOT` — raíz de salida de la grabación 24×7 (apuntar a un mount de NAS en producción, compartido con transcriber-linux). Default: `/home/sintonizador/video`.
- `SINTONIZADOR_VIDEOREC_AUTOSTART` — `true` (default): arranca junto con el archiver de CC (requiere que este último esté corriendo).
- `SINTONIZADOR_VIDEOREC_ENCODER` — `nvenc` (default) | `software` (libx264, fallback CPU si no hay GPU NVIDIA).
- `SINTONIZADOR_VIDEOREC_NVENC_LIMIT` — tope de pipelines simultáneas en NVENC antes de caer a CPU. Default: `4` (conservador — el techo real de cada GPU se verifica empíricamente, no asumir).
- `SINTONIZADOR_VIDEOREC_BITRATE` / `_MAXRATE` / `_BUFSIZE` — control de tasa H.264. Defaults: `5M`/`6M`/`10M`.
- `SINTONIZADOR_VIDEOREC_ROTATION_MINUTES` — minutos por segmento .ts. Default: `30`.
| `GET /docs`                      | Swagger UI con el schema completo |

Reproducir el stream con VLC desde otra máquina:

```
vlc http://<host>:8000/stream/0.ts
```

> Los browsers actuales no decodifican MPEG-2 + AC-3 (codecs ATSC) por
> patentes, así que el stream se consume con VLC/mpv/ffmpeg. La UI muestra
> la URL y un botón para copiarla.

Variables de entorno:
- `SINTONIZADOR_CHANNELS` — path al `channels.conf`. Default: `/home/sintonizador/channels.conf`.

## Layout

- `scripts/` — bash de instalación y diagnóstico
- `build/`   — clones de tbsdtv/media_build y tbsdtv/linux_media (gitignored)
- `logs/`    — logs de instalación
- `src/sintonizador/` — código Python (FastAPI + servicios)
- `tests/`   — tests
- `INSTALL.md` — ruta feliz de instalación
- `HISTORICO.md` — bitácora del proyecto
