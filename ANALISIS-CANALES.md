# Análisis detallado de canales ATSC OTA — Guadalajara/Jalisco

Sondeo PSIP+PMT+ffprobe de los 14 multiplexes (captura ~9s c/u por los 4 tuners).

| RF | MHz | V-chan | Callsign | Nombre comercial | Video | Audio | CC (declarado PMT) |
|---:|----:|:------|:---------|:-----------------|:------|:------|:-------------------|
| 9 | 189 | **10.1** | XHQMGU |  | mpeg2video 1920x1080 tt | ac3 2ch/stereo | — |
|  |  | **10.2** | XHQMGU2 |  | mpeg2video 1920x1080 bb | ac3 2ch/stereo | — |
|  |  | **10.3** | XHQMGU3 |  | mpeg2video 720x480 tt | ac3 2ch/stereo | — |
| 20 | 509 | **14.1** | XHSPRGA | Canal 14 | mpeg2video 1920x1080 | ac3 2ch/stereo [esp] | spa/708(digital) |
|  |  | **22.1** | XHSPRGA | Canal 22 | mpeg2video 720x480 tt | ac3 2ch/stereo [esp] | — |
|  |  | **20.1** | XHSPRGA | TV UNAM | mpeg2video 720x480 bb | ac3 2ch/stereo [esp] | esp/608(line21), esp/708(digital) |
|  |  | **14.2** | XHSPRGA | TV MIGRANTE | h264 720x480 bb | ac3 2ch/stereo [esp] | esp/608(line21), esp/708(digital) |
| 22 | 521 | **5.1** | XHGUE | Canal Cinco-XHGUE-TDT | mpeg2video 1920x1080 tt | ac3 2ch/stereo [spa], ac3 2ch/stereo [eng] | spa/708(digital) |
| 23 | 527 | **11.1** | XHPBGD | Canal 11.1 | mpeg2video 1920x1080 tt | ac3 2ch/stereo [eng] | eng/708(digital) |
|  |  | **11.2** | XHPBGD | Canal 11.2 | mpeg2video 704x480 tt | ac3 2ch/stereo [eng] | — |
| 24 | 533 | **2.1** | XHGA | Las Estrellas-XHGA-TDT | mpeg2video 1920x1080 tt | ac3 6ch/5.1(side) [spa], ac3 6ch/5.1(side) [eng] | spa/708(digital) |
|  |  | **2.2** | XHGA |  | mpeg2video 720x480 tt | ac3 2ch/stereo [spa] | — |
| 25 | 539 | **17.4** | XHGJG |  | mpeg2video 720x480 tt | ac3 2ch/stereo [eng] | — |
|  |  | **17.1** | XHGJG | JALISCOTV HD | mpeg2video 1920x1080 tt | ac3 2ch/stereo [eng] | — |
|  |  | **17.3** | XHGJG | JaliscoTV Parlamento | mpeg2video 720x480 bb | ac3 2ch/stereo [eng] | — |
|  |  | **17.2** | XHGJG | JaliscoTV | mpeg2video 720x480 tt | ac3 2ch/stereo [eng] | — |
| 26 | 545 | **9.1** | XEWO |  | mpeg2video 1920x1080 tt | ac3 2ch/stereo [spa], ac3 2ch/stereo [eng] | — |
|  |  | **9.2** | XEWO |  | mpeg2video 720x480 tt | ac3 2ch/stereo [spa] | — |
| 27 | 551 | **44.1** | XHUDG |  | mpeg2video 1920x1080 tt | ac3 2ch/stereo | — |
|  |  | **44.2** | XHUDG |  | mpeg2video 720x480 tt | ac3 2ch/stereo | — |
| 28 | 557 | **3.1** | XHCTGD |  | mpeg2video 1920x1080 tt | ac3 2ch/stereo | — |
|  |  | **3.3** | XHCTGD |  | mpeg2video 720x480 tt | ac3 2ch/stereo | — |
|  |  | **3.4** | XHCTGD |  | mpeg2video 720x480 tt | ac3 2ch/stereo | — |
| 29 | 563 | **4.1** | XHG | XHG-TDT | mpeg2video 1920x1080 tt | ac3 2ch/stereo [spa], ac3 2ch/stereo [eng] | — |
|  |  | **4.2** | XHG |  | mpeg2video 720x480 tt | ac3 2ch/stereo [spa], ac3 2ch/stereo [eng] | — |
| 31 | 575 | **7.1** | XHSFJ |  | mpeg2video 1920x1080 tt | ac3 2ch/stereo [spa] | spa/708(digital) |
|  |  | **7.2** | XHSFJ |  | mpeg2video 704x480 tt | ac3 2ch/stereo [spa] | spa/608(line21), fra/608(line21), spa/708(digital) |
| 33 | 587 | **1.1** | XHJAL |  | mpeg2video 1920x1080 tt | ac3 2ch/stereo | — |
|  |  | **?** |  |  | — | — | — |
|  |  | **1.2** | XHJAL |  | mpeg2video 704x480 tt | ac3 2ch/stereo | — |
| 34 | 593 | **6.1** | XHTDJA1 | MULTIMEDIOS | mpeg2video 1920x1080 tt | ac3 2ch/stereo [ENG] | — |
|  |  | **6.2** | XHTDJA2 | MILENIO | h264 720x480 tt | ac3 2ch/stereo [ENG] | — |
|  |  | **6.3** | XHTDJA3 | TELERITMO | h264 720x480 tt | ac3 2ch/stereo [ENG] | — |
|  |  | **6.4** | XHTDJA4 | 52MX | h264 720x480 tt | ac3 2ch/stereo [ENG] | — |
| 35 | 599 | **13.1** | XEDK | XEDK-TDT | mpeg2video 1920x1080 tt | ac3 2ch/stereo | — |
|  |  | **13.2** | XEDK | XEDK | h264 720x480 tt | ac3 2ch/stereo | — |
|  |  | **13.3** | XEDK | XEDK | h264 720x480 tt | ac3 2ch/stereo | — |

**Total: 37 subcanales en 14 multiplexes.**

## EPG (guía de programación)
- Multiplexes que **anuncian** tablas EIT (MGT): XHQMGU.
- Eventos EIT legibles capturados: **0** — la EPG OTA en GDL es prácticamente inexistente (típico de ATSC México). Para guía se necesitaría una fuente externa (ej. Schedules Direct / scraping).

## Notas
- **Códec:** los canales HD principales son MPEG-2 1080i; varios subcanales SD usan H.264 (Milenio/Teleritmo/52MX de Multimedios, XEDK 13.2/13.3, TV Migrante).
- **Audio dual (SAP esp+eng):** Canal 5 (XHGUE), Las Estrellas (XHGA), XEWO 9.1, XHG 4.1. Las Estrellas 2.1 además trae **AC-3 5.1**.
- **Idioma de audio poco confiable:** muchas emisoras marcan 'eng'/'ENG' en el descriptor aunque el contenido es español (Multimedios, JaliscoTV, XHPBGD). No fiarse de esa etiqueta.
- **CC:** la columna refleja lo *declarado* en el PMT (capacidad). La presencia real depende del programa al aire; ccextractor sobre el video puede encontrar 608 aunque el PMT no lo declare, y viceversa.
- **SPR (509 MHz):** un solo mux federal con 4 servicios independientes (Canal 14, Canal 22, TV UNAM, TV Migrante).