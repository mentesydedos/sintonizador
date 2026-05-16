"""Modelos pydantic serializables al cliente."""

from __future__ import annotations

from pydantic import BaseModel, Field


class TunerView(BaseModel):
    """Vista por cable de un TunerSnapshot."""

    adapter: int
    timestamp: float
    available: bool

    frequency_hz: int | None = None
    delivery_system: str | None = None
    modulation: str | None = None

    has_signal: bool = False
    has_carrier: bool = False
    has_lock: bool = False
    lock_raw: int = 0

    signal_dbm: float | None = None
    cnr_db: float | None = None
    cnr_sentinel: bool = Field(
        default=False,
        description="True si la última lectura cayó en el valor centinela del lgdt3306a (1.29 dB); cnr_db es None en ese caso",
    )

    error: str | None = None


class StatsMessage(BaseModel):
    """Mensaje que se manda por WS: lista de tuners + timestamp del batch."""

    type: str = "stats"
    timestamp: float
    tuners: list[TunerView]


class ChannelView(BaseModel):
    """Una entrada del catálogo (parseada de channels.conf)."""

    id: int  # índice en el catálogo, ID único en este server (no es persistente)
    name: str
    vchannel: str | None = None
    service_id: int | None = None
    video_pid: int | None = None
    audio_pid: int | None = None
    frequency_hz: int
    delivery_system: str  # "ATSC", "DVBC/ANNEX_B", ...
    modulation: str  # "VSB/8", "QAM/256", ...


class ChannelsResponse(BaseModel):
    """Respuesta de GET /channels."""

    total: int
    channels: list[ChannelView]


class MultiplexView(BaseModel):
    """Un multiplex agrupando todos sus subcanales (mismo frequency_hz)."""

    frequency_hz: int
    delivery_system: str
    modulation: str
    channels: list[ChannelView]


class MultiplexesResponse(BaseModel):
    """Respuesta de GET /multiplexes — catálogo agrupado por frecuencia."""

    total: int  # cantidad de multiplexes únicos
    multiplexes: list[MultiplexView]


class TuneRequest(BaseModel):
    """Body de POST /tuners/{n}/tune."""

    channel_id: int = Field(description="ID de canal del catálogo (índice en GET /channels)")
