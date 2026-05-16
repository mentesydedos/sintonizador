"""Parser del formato DVBV5 de `channels.conf` (salida de `dvbv5-scan`).

Formato:

    [CHANNEL_NAME]
        DELIVERY_SYSTEM = ATSC
        FREQUENCY = 189028615
        MODULATION = VSB/8
        VCHANNEL = 10.1
        SERVICE_ID = 1
        VIDEO_PID = 34
        AUDIO_PID = 35

    [CHANNEL_NAME2]
        ...
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from pathlib import Path

from sintonizador.dvb import constants as C

log = logging.getLogger(__name__)


# Mapeo string → enum del kernel para los campos que cambian al tunear.
_DELIVERY_SYSTEM_FROM_STR = {
    "ATSC": C.SYS_ATSC,
    "DVBC/ANNEX_A": C.SYS_DVBC_ANNEX_A,
    "DVBC/ANNEX_B": C.SYS_DVBC_ANNEX_B,
    "DVBC/ANNEX_C": C.SYS_DVBC_ANNEX_C,
    "DVBT": C.SYS_DVBT,
    "DVBS": C.SYS_DVBS,
    "DVBS2": C.SYS_DVBS2,
}

_MODULATION_FROM_STR = {
    "QPSK": C.QPSK,
    "QAM/16": C.QAM_16,
    "QAM/32": C.QAM_32,
    "QAM/64": C.QAM_64,
    "QAM/128": C.QAM_128,
    "QAM/256": C.QAM_256,
    "QAM/AUTO": C.QAM_AUTO,
    "VSB/8": C.VSB_8,
    "VSB/16": C.VSB_16,
}


@dataclass(frozen=True, slots=True)
class Channel:
    """Un canal del catálogo, parseado de channels.conf.

    Los IDs numéricos (delivery_system, modulation) ya están traducidos al
    enum del kernel — listos para pasar a `Frontend.tune()`.
    """

    name: str  # nombre del [SECTION]
    vchannel: str | None  # "10.1", "5.2", etc. (PSIP virtual channel)
    service_id: int | None
    video_pid: int | None
    audio_pid: int | None
    frequency_hz: int
    delivery_system: int  # kernel enum (SYS_ATSC, etc.)
    delivery_system_name: str  # string original ("ATSC")
    modulation: int  # kernel enum (VSB_8, etc.)
    modulation_name: str  # string original ("VSB/8")
    extra: dict[str, str] = field(default_factory=dict)


def parse(text: str) -> list[Channel]:
    """Parsea texto en formato DVBV5 channels.conf."""
    channels: list[Channel] = []
    current_name: str | None = None
    current_kv: dict[str, str] = {}

    def flush() -> None:
        if current_name is None:
            return
        ch = _build_channel(current_name, current_kv)
        if ch is not None:
            channels.append(ch)

    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("[") and line.endswith("]"):
            flush()
            current_name = line[1:-1].strip()
            current_kv = {}
            continue
        if "=" in line and current_name is not None:
            key, _, value = line.partition("=")
            current_kv[key.strip()] = value.strip()
    flush()
    return channels


def parse_file(path: Path | str) -> list[Channel]:
    """Lee `path` y devuelve la lista de canales parseados."""
    return parse(Path(path).read_text(encoding="utf-8"))


def _build_channel(name: str, kv: dict[str, str]) -> Channel | None:
    """Convierte un dict de KEY=VALUE en un Channel. Devuelve None si falta algo crítico."""
    try:
        freq = int(kv["FREQUENCY"])
    except (KeyError, ValueError):
        log.warning("channel %r: FREQUENCY ausente o inválida — descartando", name)
        return None

    delsys_str = kv.get("DELIVERY_SYSTEM", "")
    delsys = _DELIVERY_SYSTEM_FROM_STR.get(delsys_str)
    if delsys is None:
        log.warning("channel %r: DELIVERY_SYSTEM=%r desconocido — descartando", name, delsys_str)
        return None

    mod_str = kv.get("MODULATION", "")
    mod = _MODULATION_FROM_STR.get(mod_str)
    if mod is None:
        log.warning("channel %r: MODULATION=%r desconocido — descartando", name, mod_str)
        return None

    known_keys = {
        "FREQUENCY", "DELIVERY_SYSTEM", "MODULATION",
        "VCHANNEL", "SERVICE_ID", "VIDEO_PID", "AUDIO_PID",
    }
    extra = {k: v for k, v in kv.items() if k not in known_keys}

    return Channel(
        name=name,
        vchannel=kv.get("VCHANNEL"),
        service_id=_int_or_none(kv.get("SERVICE_ID")),
        video_pid=_int_or_none(kv.get("VIDEO_PID")),
        audio_pid=_int_or_none(kv.get("AUDIO_PID")),
        frequency_hz=freq,
        delivery_system=delsys,
        delivery_system_name=delsys_str,
        modulation=mod,
        modulation_name=mod_str,
        extra=extra,
    )


def _int_or_none(s: str | None) -> int | None:
    if s is None or s == "":
        return None
    try:
        return int(s)
    except ValueError:
        return None


def vchannel_sort_key(vchannel: str | None) -> tuple[int, int]:
    """Parsea 'major.minor' a tupla para ordenar numéricamente.

    '10.1' → (10, 1), '6.2' → (6, 2), '10' → (10, 0), None → (10**9, 0).
    Canales sin vchannel quedan al final.
    """
    if not vchannel:
        return (10**9, 0)
    parts = vchannel.split(".", 1)
    try:
        major = int(parts[0])
    except (ValueError, IndexError):
        return (10**9, 0)
    minor = 0
    if len(parts) == 2:
        try:
            minor = int(parts[1])
        except ValueError:
            minor = 0
    return (major, minor)


def channel_sort_key(c: Channel) -> tuple[int, int, int]:
    """Orden canónico del catálogo: por frequency_hz, luego por vchannel."""
    major, minor = vchannel_sort_key(c.vchannel)
    return (c.frequency_hz, major, minor)
