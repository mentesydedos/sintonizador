"""Detección de adaptadores DVB presentes en el sistema.

El proyecto arrancó con 4 tuners (1 TBS6704). Al añadir una segunda tarjeta
aparecen `adapter4..7`. En vez de codificar `[0,1,2,3]`, descubrimos los
adaptadores reales por los nodos `/dev/dvb/adapterN/frontend0`.

Override por env `SINTONIZADOR_ADAPTERS` (lista separada por comas) para
pruebas — p.ej. simular 8 tuners en una caja con 4:
    SINTONIZADOR_ADAPTERS=0,1,2,3,4,5,6,7
"""

from __future__ import annotations

import glob
import logging
import os
import re

log = logging.getLogger(__name__)

_ADAPTER_RE = re.compile(r"/adapter(\d+)/frontend0$")
_FALLBACK = [0, 1, 2, 3]


def detect_adapters() -> list[int]:
    """Lista ordenada de índices de adaptadores DVB presentes.

    Prioridad:
      1. `SINTONIZADOR_ADAPTERS` (CSV) si está seteada.
      2. Glob de `/dev/dvb/adapter*/frontend0`.
      3. Fallback `[0,1,2,3]` (con warning) si el glob no encuentra nada
         (p.ej. permisos o /dev/dvb no montado todavía).
    """
    env = os.environ.get("SINTONIZADOR_ADAPTERS")
    if env:
        try:
            adapters = sorted({int(x) for x in env.split(",") if x.strip() != ""})
            log.info("adaptadores por env SINTONIZADOR_ADAPTERS: %s", adapters)
            return adapters
        except ValueError:
            log.warning("SINTONIZADOR_ADAPTERS=%r inválida — ignorando, usando glob", env)

    found: set[int] = set()
    for path in glob.glob("/dev/dvb/adapter*/frontend0"):
        m = _ADAPTER_RE.search(path)
        if m:
            found.add(int(m.group(1)))
    if not found:
        log.warning(
            "no se detectaron adaptadores en /dev/dvb/adapter*/frontend0 — "
            "fallback a %s", _FALLBACK,
        )
        return list(_FALLBACK)
    adapters = sorted(found)
    log.info("adaptadores detectados: %s (%d tuners)", adapters, len(adapters))
    return adapters
