# sintonizador

Software de control y monitoreo para tarjeta sintonizadora **TBS6704** (Quad ATSC/Clear-QAM PCIe) bajo Linux.

## Estado actual

- [x] Hardware detectado (TBS6704, PCI `544d:6178`, subsystem `6704:0001`)
- [x] Driver `tbsecp3` compilado e instalado contra kernel `6.8.0-107-generic`
- [x] `/dev/dvb/adapter[0-3]` operativos (4 frontends ATSC/QAMB registrados)
- [x] Scan ATSC inicial (36 canales en `channels.conf`)
- [x] Wrapper Python sobre DVB API v5 (`sintonizador.dvb.Frontend`)
- [x] FastAPI + WebSocket para los 4 tuners (`/tuners`, `/ws/stats`)
- [ ] Web UI dashboard
- [ ] Grabación programada
- [ ] Streaming HTTP MPEG-TS
- [ ] Pipeline de transcripción

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

Variables de entorno:
- `SINTONIZADOR_CHANNELS` — path al `channels.conf`. Default: `/home/sintonizador/channels.conf`.
- `SINTONIZADOR_ARCHIVE` — directorio raíz del archive 24×7. Default: `/home/sintonizador/archive`.
- `SINTONIZADOR_ROTATION_MINUTES` — minutos por archivo. Default: `30`.
- `SINTONIZADOR_ARCHIVE_AUTOSTART` — `true` (default): el archiver arranca con uvicorn. `false`: arranque vía POST /archive.
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
