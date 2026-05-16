# HISTÓRICO — Sintonizador TBS6704

Bitácora del proyecto. Sirve dos propósitos:
1. **Preservar memoria** de las decisiones y callejones sin salida — para no repetirlos.
2. **Reproducibilidad** — combinado con [INSTALL.md](INSTALL.md), permite levantar el sistema desde cero en otra máquina.

---

## Cronología

### 2026-05-07 — Setup inicial del proyecto

- Movido de `/home/transcriber/sintonizador/` a `/home/sintonizador/` (rutas absolutas a partir de acá).
- Hardware verificado vía `lspci`: TBS6704, PCI ID `544d:6178`, subsys `6704:0001`, slot `02:00.0`.
- Estructura inicial: `scripts/`, `build/`, `src/sintonizador/`, `tests/`, `logs/`.
- Primer intento de instalación con `scripts/install-tbs-driver.sh` corriendo en kernel `6.17.0-1020-oem`.
- **Resultado:** build falla en saa716x con `pci_enable_msix` y `V4L2_VERSION` undeclared. Causa: tbsdtv soporta hasta kernel 6.14, esos símbolos fueron renombrados/eliminados en 6.15+.

### 2026-05-07/08 — Iteraciones de fix sobre 6.17 (sin éxito)

- Se escribió `scripts/resume-build.sh` con dos estrategias:
  - Parche en fuente a `videodev2.h` para incluir `<linux/time_types.h>`.
  - Trim de `.config` con blanket disable + whitelist (apagar todos los `CONFIG_*=m`, re-habilitar solo el subset de TBS6704).
- Múltiples intentos (`logs/resume-1...5*.log`). Todos fallan: los símbolos del kernel siguen sin existir, el trim no puede compensarlo.
- **Decisión:** bajar el kernel.

### 2026-05-08 — Downgrade de kernel + descubrimiento del driver real

#### §1. Downgrade exitoso

Instalado `linux-image-generic` (kernel `6.8.0-107-generic`, dentro del rango soportado 4.19–6.14). Reboot en esa entrada via GRUB.

```
$ uname -r
6.8.0-107-generic
```

Nota importante: tener instalado ≠ estar arrancado. La verificación correcta es `uname -r`, no `dpkg -l | grep linux-image`.

#### §2. Build falla en `ccs-core.c` (kernel API mismatch en driver de cámara)

Primer build en 6.8 vía `sudo bash scripts/install-tbs-driver.sh`:

```
ccs-core.c:668:21: error: too many arguments to function 'pm_runtime_get_if_active'
        pm_status = pm_runtime_get_if_active(&client->dev, true);
./include/linux/pm_runtime.h:76:12: note: declared here
   76 | extern int pm_runtime_get_if_active(struct device *dev);
```

El árbol `tbsdtv/linux_media` trae la versión nueva de 2 argumentos (kernel 6.15+) pero el kernel 6.8 todavía tiene la de 1 argumento. Este es un driver de cámara CCS, **irrelevante para ATSC**.

`install-tbs-driver.sh` hace `make allyesconfig` que enciende todos los drivers, incluidos los que no compilan.

#### §3. Build falla en `saa716x_budget.c` (whitelist demasiado agresivo)

Segundo intento con `sudo bash scripts/resume-build.sh` (que hace trim + blanket disable + whitelist):

```
saa716x_budget.c:2038:39: error: implicit declaration of function 'cx24117_get_i2c_adapter'
        struct i2c_adapter *adapter = cx24117_get_i2c_adapter(fe);
```

`saa716x_budget.c` es un único `.c` que contiene código para todas las cards con bridge saa716x (TBS6704 ATSC, TBS6984 satelital, etc.). Aunque solo nos importa la rama 6704, el archivo entero tiene que compilar — y referencia `cx24117_get_i2c_adapter()` (demod DVB-S2). El whitelist de `resume-build.sh` había apagado `CONFIG_DVB_CX24117`, lo que removió la declaración del símbolo.

**Lección:** el blanket disable + whitelist es una mala idea para hardware oscuro con código compartido entre variantes.

#### §4. Build limpio funciona, pero módulo no se bindea al hardware

Tercer intento con `scripts/build-tbs-clean.sh` (estrategia: allyesconfig + apagar solo lo que rompe en 6.8, mantener todos los demods DVB):

```
[18:43] BUILD OK
[18:43] make install + depmod
[18:43] modprobe saa716x_tbs_dvb ✓ (carga)
[18:43] /dev/dvb NO existe
[18:43] dmesg: mc: Linux media interface: v0.10 (eso es todo del driver)
```

El módulo `saa716x_tbs_dvb` carga sin error, pero `dmesg` no muestra ningún mensaje de detección PCI. Pulled in cx24117 y tas2101 (demods satelitales), pero NO lgdt3306a (que es el demod ATSC del 6704). Eso fue la pista clave: si el driver bindeara al 6704, hubiera cargado lgdt3306a.

#### §5. El driver correcto es `tbsecp3`, no `saa716x_*`

Diagnóstico vía `modinfo` y `modprobe --resolve-alias`:

```
$ modprobe --resolve-alias "pci:v0000544Dd00006178sv00006704sd00000001bc04sc80i00"
tbsecp3
```

`saa716x_tbs_dvb` usa vendor `1131` (Philips/NXP) en su `id_table` — para cards basadas en el SoC SAA7160 original. El TBS6704 es vendor `544D` (TBS Technology) — una placa rebrandeada por TBS que usa su propio FPGA ECP3. Aunque el chip puente físico se llama "SAA716x" en la docu, el driver del kernel correcto es `tbsecp3` (TBS ECP3 family).

`sudo modprobe tbsecp3`:

```
TBSECP3 driver 0000:02:00.0: enabling device (0000 -> 0002)
TBSECP3 driver 0000:02:00.0: TurboSight TBS 6704(Quad ATSC/QAMB)
dvbdev: DVB: registering new adapter (TBSECP3 DVB Adapter)
TBSECP3 driver 0000:02:00.0: DVB: registering adapter 0 frontend 0 (TurboSight TBS 6704(Quad ATSC/QAMB))...
...
TBSECP3 driver 0000:02:00.0: DVB: registering adapter 3 frontend 0 (TurboSight TBS 6704(Quad ATSC/QAMB))...
TBSECP3 driver 0000:02:00.0: TurboSight TBS 6704(Quad ATSC/QAMB): PCI 0000:02:00.0, IRQ 191, MMIO 0x85100000
```

4 adapters en `/dev/dvb/adapter[0-3]` con `frontend0`/`demux0`/`dvr0`/`net0`. ✅

`modprobe --resolve-alias` con la PCI ID exacta confirmó persistencia: en futuros boots, `udev` cargará `tbsecp3` automáticamente.

---

## Decisiones técnicas clave

### Kernel: 6.8.0-107-generic (de Ubuntu 24.04 stock)

- tbsdtv soporta 4.19–6.14. 6.8 está dentro del rango.
- Hay kernels OEM `6.17.0-1017-oem` y `6.17.0-1020-oem` instalados que **no** funcionan; quedan disponibles para otros usos pero no se bootea en ellos.
- Si Ubuntu mete un kernel automático más nuevo, hay que pinear o seguir arrancando en 6.8 manualmente vía GRUB.

### Driver: tbsecp3 (NO saa716x_*)

- Vendor 544D, no 1131.
- Demod ATSC: `lgdt3306a` (lo carga `tbsecp3` cuando hace falta).
- Auto-load registrado en `modules.alias` para PCI `544d:6178`.

### Estrategia de build: `build-tbs-clean.sh`

- `allyesconfig` para arrancar de cero contra el kernel actual.
- Apaga subárboles que rompen en 6.8: cámaras (`MEDIA_CAMERA_SUPPORT`, `CONFIG_VIDEO_*`), USB tuners, RC/IR, radio, SDR, analog TV.
- Mantiene encendidos todos los demods DVB — necesarios para que `saa716x_budget.c` compile aunque para 6704 no se usen.
- NO usa el blanket-disable de `resume-build.sh`.

### Stack del servicio

- Python + FastAPI + Web UI.
- DVB API v5 directo (`/dev/dvb/adapterN/frontend0` + TS desde `dvr0`).
- 4 funciones acordadas:
  1. Monitor en vivo (signal/SNR/BER/lock × 4 tuners)
  2. Grabación programada
  3. Streaming MPEG-TS a red
  4. Transcripción audio→texto

---

## Estado de los scripts

| Script | Estado | Comentario |
|---|---|---|
| `scripts/install-tbs-driver.sh` | ❌ NO usar | `make allyesconfig` rompe en `ccs-core.c` en kernel 6.8 |
| `scripts/resume-build.sh` | ❌ NO usar | Blanket disable + whitelist apaga demods que `saa716x_budget.c` necesita |
| `scripts/build-tbs-clean.sh` | ✅ Usar este | Allyesconfig + apagar solo lo que rompe, mantener todos los demods DVB |
| `scripts/verify-driver.sh` | ✅ Usar | Corregido 2026-05-13: ahora busca `tbsecp3` (era `saa716x_tbs_dvb`). Sin sudo. Chequea PCI / módulos / `/dev/dvb` / capabilities ATSC / permisos grupo `video`. |
| `scripts/scan-channels.sh` | ✅ Usar | Driver-agnóstico. Requiere paquete `dtv-scan-tables` (agregado a INSTALL.md) y antena ATSC. Salida: `./channels.conf` en formato DVBv5. |

Los dos scripts marcados ❌ se dejan en el repo por su valor histórico y porque tienen partes útiles (parche de `videodev2.h`, lista de toggles de alto nivel), pero **no se invocan directamente**.

---

## 2026-05-14 — Archivador 24×7 de Closed Captions

Pedido del usuario: archivos de transcripción de 30 min cada uno, 24×7,
separados por subcanal, con alta precisión, tomados del CC de los multiplexes
533/557/575/587 MHz. Implementado como módulo `sintonizador.archiver` con:

- `ArchiveTarget` (config.py): una entrada por subcanal con
  adapter/freq/program_id/channel_name/vchannel/slug. Por default arma 9
  targets desde los 4 multiplexes pedidos.
- `ArchivePipeline` (pipeline.py): por subcanal abre un demux con
  TSDEMUX_TAP, lanza `stdbuf -o0 ccextractor -s -1 -pn N -in=ts -stdin
  -stdout`, parsea SRT bloque a bloque, escribe a `.srt` (verbatim) +
  `.txt` (texto con wall-clock prefix). Rotación cada
  `rotation_minutes` (default 30) en bordes naturales (HH:00, HH:30, …).
  Auto-restart si ccextractor crashea.
- `Archiver` (archiver.py): orquesta N pipelines, tunea cada adapter al
  multiplex correspondiente vía `MonitorPoller.tune`, **reserva los
  adapters** (la API de tune los rechaza con 409 mientras esté corriendo).
- API: `GET /archive` (status), `POST /archive` (start), `DELETE /archive`
  (stop). UI: panel "Archivador 24×7" en el header con grid de pipelines
  + botón Start/Stop.
- Lifespan: el archiver auto-arranca con uvicorn por default
  (`SINTONIZADOR_ARCHIVE_AUTOSTART=false` para desactivar).

**Bugs encontrados durante implementación:**

1. **`ccextractor` sin `-s` (stream mode) cierra al "EOF aparente"** del
   pipe stdin después de unos segundos sin datos densos. Síntoma: la
   pipeline reportaba "restart in 2.0s (#1)" continuamente y nunca se
   estabilizaba. Fix: agregar `-s` al comando — `stream mode, don't
   terminate on apparent EOF`.

2. **Python stdout buffer en subprocess** — corriendo el server como
   background process (`run_in_background`) sin `-u`/`PYTHONUNBUFFERED=1`,
   los logs nunca aparecían en el output file. Workaround: usar `-u`
   siempre que se corra el server fuera de TTY.

**Test e2e (rotación 1 min, ~90 segundos)**:

- 9 carpetas creadas (1 por subcanal).
- 3 períodos rotados correctamente.
- Subcanales primarios capturaron CC reales: XHGA 2.1 grabó un comercial
  de Flanax ("NO PASARÍAN SI EL DOLOR SE PUSIERA EN TU CAMINO. FLANAX
  ALIVIA RÁPIDAMENTE EL FUERTE"), XHJAL 1.1 grabó diálogo ("no por eso
  me voy a tirar encima de alguien…"), XHCTGD 3.1 grabó intro ("Dinos
  un momento en humilde, Paulina mercado."), XHSFJ 7.1 ("cuando él
  muera").
- Subcanales secundarios (1.2, 2.2, 3.3, 3.4, 7.2) generan archivos con
  solo header — esos broadcasters emiten CC únicamente en el subcanal
  primario.
- Storage: ~22 MB/día estimado para los 9 subcanales.

**Estimado para 24×7 sostenido:**

- 9 subprocesos ccextractor permanentes, ~50–100 MB RSS total.
- ~170 Mbit/s de TS parsing total (≤1 core de CPU).
- ~22 MB/día de archive (650 MB/mes), almacenamiento despreciable.
- Storage en `/home/sintonizador/archive/{vchannel}-{name}/YYYY-MM-DD_HH-MM.{srt,txt}`.

## 2026-05-13 — Inventory de canales con CC (scripts/scan-cc.py)

Pregunta del usuario: ¿cuáles canales emiten CC? Para responderla armé
`scripts/scan-cc.py`: recorre los 14 multiplexes, tunea, captura 15s y prueba
8 combinaciones de flags de ccextractor.

**Resultado (2026-05-13 ~18:25 GDL)**: **5 de 14 multiplexes** con CC
decodificable:

| Mux | Sub | Texto preview |
|---|---|---|
| 521 MHz | 5.1 XHGUE | `[gritos] - ¡Sí, bebeeé!` |
| 533 MHz | 2.1/2.2 XHGA | `Néstor, ¿cómo estás? — Buenos días, Rodrigo.` |
| 557 MHz | 3.1/3.3/3.4 XHCTGD | `[titubea] Está aburrida.` |
| 575 MHz | 7.1/7.2 XHSFJ | `[música dramática] ♪` |
| 587 MHz | 1.1/1.2 XHJAL | `(Rocío) >> No es cierto.` |

Los otros 9 multiplexes (XHQMGU 10.x, XHSPRGA 14/20/22, XHGJG 17.x, XEWO 9.x,
etc.) NO emitían CC en ese momento — la programación define si hay CC, así
que esto cambia.

**Hallazgo de encoding**: el flag `-1` (CEA-608 CC1) da texto en UTF-8 limpio
para los broadcasters mexicanos. El default mezcla CEA-608 + CEA-708 y los
708 llegan con encoding roto (`�S�` en vez de `¡Sí`).

→ Update en `LiveCCSession`: `ccextractor` ahora se invoca con `-1` por
defecto para evitar mojibake.

## 2026-05-13 — Live Closed Captions vía WebSocket

Sesión continua de extracción en vivo de CC. Pipeline:

    demux (TSDEMUX_TAP) → ccextractor en pipe → parser SRT → broadcast WS

- `sintonizador.extract.live_cc.LiveCCSession`: ref-counted; arranca al primer
  cliente WS, se apaga al último.
- `WS /ws/cc/{n}`: cliente suscribe a la sesión del adapter. Recibe eventos
  `{type, ...}`:
  - `status` (running/stopped)
  - `cc` (seq, start_ms, end_ms, text)
  - `error` (message)
- UI: panel `<details>` "Closed Captions en vivo" por card. Toggle abre
  WS; cerrarlo desconecta. Auto-scroll inteligente (pausa si el user
  scrollea arriba).

**Bugs / setup encontrados:**

1. **Flags ccextractor v0.94**: en este apt es `--nofontcolor` (un guion
   seguido, sin guion intermedio), no `--no-fontcolor`. Idem `--norollup`.
   v0.94 desde Ubuntu 24.04 es la versión vieja-ish; las builds nuevas en
   github usan otros nombres.
2. **Block buffering**: ccextractor escribe stdout con buffer de 4 KB
   cuando se pipea. Para live no funciona — se ven los eventos en
   ráfagas con 5-15 s de retraso. Fix: prepender `stdbuf -o0 -e0` al
   comando para deshabilitar buffer.
3. **CC en los broadcasters mexicanos GDL**: probado con XHQMGU 10.1,
   XHGUE 5.1, XEDK 13.1 (30 s cada uno). En todos:
   - `Total user data fields: 884` (XHQMGU/30s) → user_data presente
   - `HDTV type user data fields: 884` → sectores CC reservados
   - **`No captions were found in input`** → payload vacío o variante
     que ccextractor 0.94 no decodifica
   
   Conclusión: la pipeline funciona mecánicamente; los broadcasters
   parecen no emitir CC efectivo en estos horarios (o emiten variante
   no soportada por v0.94). Para forzarlo, opciones futuras:
   - Compilar ccextractor desde HEAD (mejor soporte CEA-708).
   - Probar `ffmpeg -f lavfi -i "movie=ts[out0+subcc]"` como alternativa.
   - Probar en horarios distintos (programación con CC más probable
     en mañana o noches).

## 2026-05-13 — Info técnica + Closed Captions por card

Cuarto entregable. Aprovechando `DMX_OUT_TSDEMUX_TAP`, podemos correr
extractores en paralelo al stream a VLC sin interferir.

**Cambios:**

- `Demux.set_filter_all_pids_tsdemux_tap(buffer_kb=512)` — TS sale por el FD
  del demux directamente, no por `dvr0`. Múltiples consumers concurrentes
  posibles.
- `sintonizador.extract` paquete nuevo:
  - `capture.py`: `capture_ts_seconds(adapter, seconds, out_path)` — graba a
    tempfile vía TSDEMUX_TAP, sin bloquear el event loop.
  - `tools.py`: wrappers async de `ffprobe` (JSON con programs/streams) y
    `ccextractor` (SRT crudo). Ambos con degradación elegante si no están
    instalados.
- API: `GET /tuners/{n}/info?seconds=N` (default 3s, máximo 30s).
  Captura → ffprobe + ccextractor en paralelo → JSON.
- UI: `<details>` colapsible por card, "Info técnica + Closed Captions",
  lazy-load al primer abrir + botón ↻ refresh.

**Dependencia opcional:** `ccextractor` debe instalarse con
`sudo apt install ccextractor` para tener los Closed Captions. Sin él, el
endpoint sigue funcionando pero la sección CC dice "no instalado".

**Hallazgo curioso de codecs por broadcaster** (mismo PSIP catalog, distintas
realidades):

| Multiplex | Composición típica |
|---|---|
| 189 MHz (XHQMGU) | 3 × MPEG-2 1080p (broadcaster clásico) |
| 593 MHz (XHTDJA) | 1 × MPEG-2 1080p + 3 × **H.264 480p** (broadcaster moderno) |

El campo `closed_captions: 1` en el stream view de ffprobe indica que el
program emite CC. No siempre prendido — varía con el contenido en el aire.
ccextractor extrae texto solo si efectivamente hay CC durante la ventana
de captura.

## 2026-05-13 — Orden ASC + agrupación por multiplex en la UI

Mejora pedida por el usuario: poder monitorear simultáneamente todos los
subcanales que cabe en los 4 tuners.

**Análisis previo:**

Con 4 tuners se cubren 4 multiplexes simultáneos. Como cada multiplex llega
con TODOS sus subcanales en un único TS, el máximo en *esta locación* (catálogo
Guadalajara/Jalisco) son **15 subcanales simultáneos** eligiendo los 4
multiplexes más densos:

| Multiplex | Subcanales | Notas |
|---|---|---|
| 509.029 MHz | 4 (XHSPRGA 14.1/14.2/20.1/22.1) | El mismo broadcaster en 4 vchannels |
| 539.029 MHz | 4 (XHGJG 17.1/17.2/17.3/17.4) | |
| 593.029 MHz | 4 (XHTDJA1-4 en 6.1/6.2/6.3/6.4) | |
| 189.029 MHz | 3 (XHQMGU 10.1/10.2/10.3) | |
| **TOTAL** | **15** simultáneos en VLC | |

**Patrón de uso para multi-vista:**
- Cada tuner asignado a un multiplex distinto.
- Abrir N instancias de VLC con la MISMA URL `/stream/{n}.ts` y elegir
  program distinto en cada una (Reproducción → Programa).
- NO tunear el mismo multiplex en dos adapters (splitter interno divide
  la señal y rompe el lock — ya documentado).

**Cambios implementados:**

- `channels.py`: `channel_sort_key(c) = (frequency_hz, vch_major, vch_minor)`.
  Catálogo se carga ya ordenado en `_load_catalog`.
- `GET /multiplexes` nuevo endpoint: catálogo agrupado por frecuencia RF.
- UI dropdown: `<optgroup label="509.029 MHz · 4 subcanales">` por multiplex,
  visualmente obvio qué subcanales comparten tuner.
- UI por card (post-tune): panel "Subcanales en este multiplex" con
  `vchannel · name · program N` para cada subcanal del multiplex actual, +
  hint de cómo elegir program en VLC.

**Hallazgo de PSIP/PMT mexicano:** los `service_id` no son 1..N contiguos.
Ejemplo en 509 MHz: `14.1 = program 1`, `14.2 = program 7`, `20.1 = program 4`,
`22.1 = program 3`. Si la UI mostrara solo "program 1..N", el usuario en VLC
no sabría qué pickear. La UI ahora muestra el SID real leído por dvbv5-scan
del PMT.

## 2026-05-13 — Stream MPEG-TS raw (Opción A: VLC externo)

Tercer entregable: el sintonizador ya transmite video.

- `sintonizador.dvb.demux` — wrapper para `DMX_SET_PES_FILTER` con PID `0x2000`
  (all-PIDs), output `TS_TAP`, flag `IMMEDIATE_START`. Sin esto, `dvr0` no
  entrega bytes. También expone `DMX_SET_BUFFER_SIZE` (default 512 KB para
  tolerar pausas del consumer).
- Endpoint `GET /stream/{n}.ts` con `Content-Type: video/mp2t`, `StreamingResponse`
  de FastAPI. Devuelve 409 si el adapter no está sintonizado, evitando que el
  cliente cuelgue por nada.
- Read non-blocking de `dvr0` corre en thread pool (`loop.run_in_executor`)
  con reintento en EAGAIN. Chunk: 188 × 200 = 37.6 KB.
- UI: cuando el adapter está tuneado, aparece debajo de los botones una fila
  con la URL pegable + botón 📋 copy (clipboard API, con fallback a
  `execCommand`) + botón ↗ "abrir directo".

**Por qué solo VLC externo y no en-browser:** ATSC en MX/US transporta video
MPEG-2 + audio AC-3. Los browsers (Chromium/Firefox) no decodifican esos codecs
por patentes — independiente del contenedor TS/MP4/HLS. Para playback embebido
habría que transcodear a H.264 + AAC con ffmpeg (cada stream cuesta ~1 core o
NVENC en GPU); queda como opción B para más adelante.

**Verificación:**

- 3 segundos de stream → 7.14 MB capturados (~19 Mbps, el cap ATSC pleno).
- TS sync `0x47` cada 188 bytes ✓.
- ffprobe del TS streamed reconoce 3 programas en el multiplex 189 MHz:
  XHQMGU 10.1 (1080i MPEG-2 + AC-3 + CC), 10.2 (1080i + AC-3), 10.3 (480p + AC-3 + CC).

## 2026-05-13 — Tune control desde la UI

Segundo entregable. Lo nuevo:

- `Frontend.tune(freq, delsys, mod)` y `.clear()` — `FE_SET_PROPERTY` (`_IOW`,
  no `_IOR`). Cambió el default de `Frontend.open()` a `O_RDWR` (tune lo
  requiere; lecturas siguen funcionando igual). Param `read_only=True` para
  clientes que no van a tunear.
- `src/sintonizador/channels.py` — parser de DVBV5 channels.conf. Devuelve
  `Channel` con `delivery_system`/`modulation` ya traducidos al enum del
  kernel (listo para `Frontend.tune()`).
- `MonitorPoller.tune(adapter, …)` y `.clear(adapter)` — reusan el handle
  RDWR ya abierto por el poller. No hace falta cerrar/reabrir.
- API:
  - `GET /channels` — catálogo cargado al startup (path en
    `SINTONIZADOR_CHANNELS`, default `/home/sintonizador/channels.conf`).
  - `POST /tuners/{n}/tune` — body `{channel_id: int}`, devuelve 202.
  - `DELETE /tuners/{n}/tune` — 202.
- UI: dropdown con los 36 canales + botones Tune/Release por card. El estado
  de lock viene por WS, no necesita refresh manual.

**Hallazgos de hardware durante el tune control:**

- **DTV_CLEAR no resetea los bits de lock** (`raw=0x1f` queda stale aún
  después del clear). Pero `frequency_hz` sí pasa a 0. Fix en el poller:
  cuando `tune.frequency_hz == 0`, enmascarar TODO (lock, signal, cnr) a
  None/False en el snapshot. Sino la UI mostraría tuners "locked" sin estar
  tuneados.
- **Splitter interno del TBS6704**: los 4 tuners comparten una entrada de
  antena. Cuando dos tuners se sintonizan al mismo multiplex, el splitter
  divide la potencia (-3 dB c/u). En la prueba: adapter 0 ya lockeado en 189
  MHz perdió lock al tunear adapter 2 al mismo. C/N pasó de 16 a 5.2 dB,
  imposible de decodificar. **Para grabación/streaming simultáneo, asignar
  multiplexes distintos a tuners distintos** — un mismo multiplex puede
  estar tuneado por **un solo** adapter y todos los servicios (10.1, 10.2,
  10.3) se demultiplexan del mismo TS por PID. No hay razón para tunear el
  mismo multiplex dos veces.
- **CNR sentinel apareció más seguido tras tune fresh**: la primera lectura
  post-lock tiende a salir con `value=1290` (1.29 dB exacto), filtrada por
  el poller. En el snapshot del test se ve `cnr_db=None` con
  `cnr_sentinel=False` (porque scale tampoco era DECIBEL en ese sample —
  llegó NOT_AVAILABLE).

## 2026-05-13 — MVP del monitor: FastAPI + WebSocket sobre los 4 tuners

Primer entregable Python. Layout (paquete `sintonizador` en `src/`):

```
src/sintonizador/
  dvb/
    constants.py    — ioctls, enums, ctypes structs (dtv_property, dtv_fe_stats, …)
    frontend.py     — Frontend class: open/read_status/read_stats/read_tune_info
  monitor/
    poller.py       — MonitorPoller: asyncio loop, 500ms, pubsub, filtro sentinel lgdt3306a
  api/
    app.py          — FastAPI: GET /tuners, WS /ws/stats
    models.py       — pydantic TunerView, StatsMessage
  main.py           — uvicorn entry
```

Verificado end-to-end: `python -m sintonizador.main` → 8000 → `curl /tuners`
y WS `/ws/stats` retornan JSON con los 4 tuners. Snapshot del WS:

```json
{"adapter":0,"has_lock":true,"signal_dbm":-34.0,"cnr_db":16.34,
 "frequency_hz":189028615,"delivery_system":"ATSC","modulation":"8-VSB"}
```

**Bugs encontrados y resueltos durante esta sesión:**

1. **`struct dtv_stats` tiene `__u8 scale`, no `__u32`.** Asumí u32 por
   simetría visual con el resto del API. Causó valores totalmente basura en
   las lecturas (`scale=4287031297`, `value=21395396764893183`). Confirmé
   con `gcc + sizeof()` que `sizeof(struct dtv_fe_stats) = 37` bytes
   (no 49), lo que solo cierra con `scale=u8`. Lección: para wrapper ctypes
   de structs del kernel, **compilar un C que imprima sizeof y offsetof
   antes** en vez de adivinar.

2. **`pkill` salió con exit 144 (no es error).** Es `128 + 16 = SIGURG`,
   raro pero no significa fallo — pgrep posterior confirmó que el proceso
   estaba muerto. Solo telemetría rara.

**Hallazgos de hardware durante el monitor:**

- adapter 0 sigue lockeado en 189 MHz desde el `dvbv5-zap` previo — el
  estado persiste a través de opens read-only. Los signal/CNR se actualizan
  en vivo (signal varió -32 a -35 dBm, CNR 15–16 dB) — buena prueba de
  que el polling muestrea lecturas frescas, no cacheadas.
- adapters 1–3 sin tune previo reportan `signal=0 dBm` con
  `scale=DECIBEL`, no `NOT_AVAILABLE`. Es lectura real del chip (sin
  portadora detectada), no un error.
- El bug del C/N sentinel (1.29 dB) **no apareció** durante este test —
  todas las lecturas del CNR fueron reales. El filtro está implementado
  igual por las dudas (`monitor/poller.py:LGDT3306A_CNR_SENTINEL_MDB`).

## 2026-05-13 — Captura end-to-end de XHQMGU 10.1 (189 MHz)

Test de tuneo + grabación con `dvbv5-zap -a 0 -P -t 10 -o /tmp/test.ts "XHQMGU"`:

- 24.3 MB en 10 s = ~19 Mbps (cerca del cap ATSC de 19.4 Mbps).
- 2 programas decodificados por ffprobe:
  - **XHQMGU 10.1**: MPEG-2 1920×1080 @ 29.97 fps + AC-3 stereo + **Closed Captions** 🎉
  - **XHQMGU 10.2**: MPEG-2 1920×1080 @ 29.97 fps interlaced
- Hallazgo importante: el bug C/N del lgdt3306a NO es constante — durante esta
  captura las 3 lecturas mostraron `1.29 / 1.29 / 20.43 dB`. El valor `1.29`
  exacto es centinela; valores reales sí salen ocasionalmente. Implicancia para
  el wrapper Python: muestrear varias veces, descartar 1.29 exactos, promediar
  el resto.
- Descubrimiento útil para pipeline de transcripción: los CC vienen embebidos
  en el video MPEG-2 (estándar ATSC EIA-608/EIA-708 sobre user_data). Es decir,
  para programación en vivo podemos usar `ccextractor` y saltear Whisper.

## 2026-05-13 — Scan ATSC inicial exitoso

Primer scan completo (`scripts/scan-channels.sh 0`) tras el fix de input-format
(DVBV5 vs CHANNEL) y la instalación de `dtv-scan-tables`. Antena conectada al
adapter 0.

**Resultado:** 36 servicios sobre 14 multiplex (1 VHF-Hi + 13 UHF). Broadcasters
de Guadalajara/Jalisco (XEDK, XHJAL, XHGJG, XHTDJA1-4, XHGA, XHQMGU, etc.).
Guardado en `/home/sintonizador/channels.conf` (formato DVBV5).

**Errores cosméticos del scanner que confunden pero son benignos:**

1. `ERROR command INVERSION (6) not found during store` — la tabla declara
   `INVERSION = AUTO` heredado de DVB. ATSC no tiene concepto de inversión
   espectral; el frontend rechaza la prop, dvbv5-scan avisa, continúa.
2. `ERROR dvb_read_sections: no data read on section filter` +
   `ERROR error while reading the NIT table` — dvbv5-scan busca el NIT
   (Network Information Table) que es DVB-only. ATSC usa PSIP (PAT/PMT/VCT).
   El scanner igual lee PSIP correctamente (channels.conf llega con nombres
   reales, VCHANNEL, service IDs y PIDs).
3. `C/N = 1.29 dB postBER = 1.00` constante en cada Lock — **bug del driver
   lgdt3306a**: post-lock reporta C/N estático. Pre-lock (cuando ve RF pero no
   logra sync, status `0x01`) sí mide bien (29.53 dB, 18.42 dB, 15.93 dB en
   frecuencias sin lock). El wrapper Python debe **descartar las lecturas de
   C/N tras lock o leerlas de otra propiedad** (`FE_STAT_SIGNAL_STRENGTH`,
   `FE_STAT_CNR` con scale check, etc.).

## Próximos pasos

1. ~~Scan ATSC inicial~~ ✅ 36 canales en GDL/Jalisco.
2. Empezar el wrapper Python sobre DVB API v5 en `src/sintonizador/dvb/` —
   debe tener en cuenta el bug de C/N post-lock del lgdt3306a.
3. FastAPI con endpoints `/tuners`, `/channels`, `/scan`, `/record`, `/stream`
   + WebSocket `/ws/stats`.
4. Web UI dashboard.
5. Pipeline de grabación / streaming / transcripción.

---

## Pitfalls para futuras instalaciones / migraciones

- **Verificar kernel arrancado, no kernel instalado.** `uname -r` es la fuente de verdad.
- **Buscar el driver real por PCI ID, no por nombre del chip.** `modprobe --resolve-alias "pci:v...d...sv...sd...bc*sc*i*"` con la ID exacta es el atajo definitivo. Cuando un módulo "carga" pero `/dev/algo` no aparece, casi seguro es vendor/subsys mismatch.
- **Si la card es out-of-tree y kernel ≥6.15, esperar/parchear.** No vale la pena pelearse con `pm_runtime_get_if_active`, `pci_enable_msix`, `V4L2_VERSION` a mano.
- **El parche `videodev2.h` para `<linux/time_types.h>` se aplica al árbol `media/` (fuente).** El sync de `media_build` (`make dir DIR=../media`) lo copia a `v4l/`, así sobrevive a `apply_patches`.
- **`make distclean` en `media_build/` no toca `../media/`** — el parche de `videodev2.h` sobrevive entre rebuilds.
- **Firmware TBS:** se descarga desde `http://www.tbsdtv.com/download/document/linux/tbs-tuner-firmwares_v1.0.tar.bz2` y se extrae en `/lib/firmware/`. Para TBS6704 ATSC en particular puede no hacer falta firmware separado (lgdt3306a tiene su tabla embebida), pero el tarball es barato y conviene tenerlo.
