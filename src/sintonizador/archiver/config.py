"""Configuración del archiver: qué multiplexes archivar, asignación de adapters."""

from __future__ import annotations

import re
from dataclasses import dataclass

from sintonizador.channels import Channel


# Frecuencias por defecto pedidas por el usuario: 533/557/575/587 MHz.
# Los 4 multiplexes que SÍ emiten CC en GDL según el scan 2026-05-13:
#   533 → XHGA 2.1/2.2
#   557 → XHCTGD 3.1/3.3/3.4
#   575 → XHSFJ 7.1/7.2
#   587 → XHJAL 1.1/1.2
# Total: 9 subcanales en 4 tuners.
DEFAULT_MULTIPLEX_FREQS_HZ: list[int] = [
    533028615,
    557028615,
    575028615,
    587028615,
]


_SLUG_SANITIZE = re.compile(r"[^A-Za-z0-9._-]+")


@dataclass(frozen=True, slots=True)
class ArchiveTarget:
    """Una subcanal a archivar (= un program_id dentro de un multiplex)."""

    adapter: int
    frequency_hz: int
    delivery_system: int  # enum kernel (C.SYS_ATSC=11, etc.)
    modulation: int  # enum kernel (C.VSB_8=7 para ATSC OTA)
    program_id: int  # service_id del PMT — lo que pasamos a `ccextractor -pn`
    channel_name: str  # "XHGA", "XHCTGD", etc.
    vchannel: str  # "2.1", "3.1", etc.

    @property
    def slug(self) -> str:
        """Identificador filename-safe: '2.1-XHGA'."""
        raw = f"{self.vchannel}-{self.channel_name}"
        return _SLUG_SANITIZE.sub("_", raw)


def build_targets(
    channels: list[Channel],
    multiplex_freqs_hz: list[int] | None = None,
) -> list[ArchiveTarget]:
    """Construye la lista de targets a partir del catálogo y la lista de freqs.

    Asigna adapter 0..N-1 a cada multiplex en orden ascendente de frecuencia.
    Dentro de un multiplex, una target por cada `Channel` con esa frecuencia
    (preserva el orden que viene del catálogo, que ya está ordenado por vchannel).
    """
    if multiplex_freqs_hz is None:
        multiplex_freqs_hz = list(DEFAULT_MULTIPLEX_FREQS_HZ)

    targets: list[ArchiveTarget] = []
    for adapter, freq in enumerate(sorted(multiplex_freqs_hz)):
        for ch in (c for c in channels if c.frequency_hz == freq):
            if ch.service_id is None:
                # Sin service_id no podemos pasar -pn → sería ambiguo
                continue
            targets.append(
                ArchiveTarget(
                    adapter=adapter,
                    frequency_hz=freq,
                    delivery_system=ch.delivery_system,
                    modulation=ch.modulation,
                    program_id=ch.service_id,
                    channel_name=ch.name,
                    vchannel=ch.vchannel or "?",
                )
            )
    return targets
