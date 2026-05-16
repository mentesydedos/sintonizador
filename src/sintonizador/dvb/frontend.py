"""Wrapper de un frontend DVB (`/dev/dvb/adapterN/frontend0`).

Lectura no-bloqueante de status + propiedades v5. Sin tuning todavía
(eso queda para fase 2).
"""

from __future__ import annotations

import ctypes
import errno
import fcntl
import os
from dataclasses import dataclass

from sintonizador.dvb import constants as C


@dataclass(frozen=True, slots=True)
class LockState:
    """Bitfield FE_HAS_* descompuesto en flags + raw."""

    raw: int

    @property
    def has_signal(self) -> bool:
        return bool(self.raw & C.FE_HAS_SIGNAL)

    @property
    def has_carrier(self) -> bool:
        return bool(self.raw & C.FE_HAS_CARRIER)

    @property
    def has_viterbi(self) -> bool:
        return bool(self.raw & C.FE_HAS_VITERBI)

    @property
    def has_sync(self) -> bool:
        return bool(self.raw & C.FE_HAS_SYNC)

    @property
    def has_lock(self) -> bool:
        return bool(self.raw & C.FE_HAS_LOCK)

    @property
    def timed_out(self) -> bool:
        return bool(self.raw & C.FE_TIMEDOUT)


@dataclass(frozen=True, slots=True)
class StatSample:
    """Una lectura de un DTV_STAT_* con su escala asociada."""

    scale: int  # FE_SCALE_*
    value: int  # raw int64 — interpretación depende de scale

    @property
    def available(self) -> bool:
        return self.scale != C.FE_SCALE_NOT_AVAILABLE

    @property
    def db(self) -> float | None:
        """Convierte de mdB (scale=DECIBEL) a dB. None si la escala no aplica."""
        if self.scale != C.FE_SCALE_DECIBEL:
            return None
        return self.value / 1000.0

    @property
    def relative(self) -> float | None:
        """Convierte de 0..65535 (scale=RELATIVE) a fracción 0..1. None si la escala no aplica."""
        if self.scale != C.FE_SCALE_RELATIVE:
            return None
        return self.value / 65535.0


@dataclass(frozen=True, slots=True)
class TuneInfo:
    """Parámetros actuales del frontend (cero si no está sintonizado)."""

    frequency_hz: int  # 0 si no hay tune previo
    delivery_system: int  # SYS_ATSC=11, SYS_DVBC_ANNEX_B=18, etc.
    modulation: int  # VSB_8=4 para ATSC OTA típico


@dataclass(frozen=True, slots=True)
class FrontendStats:
    """Snapshot de un frontend en un instante."""

    lock: LockState
    signal_strength: list[StatSample]  # típicamente 1 sample en dBm (scale=DECIBEL)
    cnr: list[StatSample]  # típicamente 1 sample en dB (scale=DECIBEL)
    pre_ber_error: StatSample | None = None  # counter
    pre_ber_total: StatSample | None = None  # counter


class FrontendError(OSError):
    """Error específico de un frontend (envuelve OSError con path)."""


class Frontend:
    """Acceso de lectura a `/dev/dvb/adapter{N}/frontend{M}`.

    Uso típico:

        with Frontend(adapter=0) as fe:
            stats = fe.read_stats()
            print(stats.lock.has_lock, stats.signal_strength[0].db)
    """

    def __init__(self, adapter: int, frontend: int = 0, read_only: bool = False) -> None:
        self.adapter = adapter
        self.frontend = frontend
        self.read_only = read_only
        self.path = f"/dev/dvb/adapter{adapter}/frontend{frontend}"
        self._fd: int | None = None

    # --- ciclo de vida ---

    def open(self) -> None:
        if self._fd is not None:
            return
        # FE_SET_PROPERTY (tune) requiere O_RDWR. Por defecto abrimos así para
        # que el mismo handle sirva para monitor + tune. read_only=True solo
        # para clientes que no van a tunear (tests, diagnóstico).
        flags = os.O_NONBLOCK | (os.O_RDONLY if self.read_only else os.O_RDWR)
        try:
            self._fd = os.open(self.path, flags)
        except OSError as e:
            raise FrontendError(e.errno, f"opening {self.path}: {e.strerror}", self.path) from e

    def close(self) -> None:
        if self._fd is None:
            return
        try:
            os.close(self._fd)
        finally:
            self._fd = None

    def __enter__(self) -> Frontend:
        self.open()
        return self

    def __exit__(self, *_exc: object) -> None:
        self.close()

    @property
    def is_open(self) -> bool:
        return self._fd is not None

    # --- lecturas ---

    def read_status(self) -> LockState:
        """FE_READ_STATUS — bitfield FE_HAS_*."""
        self._require_open()
        buf = ctypes.c_uint32(0)
        try:
            fcntl.ioctl(self._fd, C.FE_READ_STATUS, buf)
        except OSError as e:
            raise FrontendError(e.errno, f"FE_READ_STATUS on {self.path}: {e.strerror}", self.path) from e
        return LockState(raw=buf.value)

    def read_properties(self, *cmds: int) -> dict[int, C.DtvProperty]:
        """FE_GET_PROPERTY para los `cmds` dados. Devuelve dict cmd → DtvProperty.

        El llamador interpreta cada DtvProperty.u según el tipo de cmd
        (data simple vs st stats vs buffer).
        """
        self._require_open()
        if not cmds:
            return {}
        n = len(cmds)
        props_array = (C.DtvProperty * n)()
        for i, cmd in enumerate(cmds):
            props_array[i].cmd = cmd
        header = C.DtvProperties()
        header.num = n
        header.props = ctypes.cast(props_array, ctypes.POINTER(C.DtvProperty))
        try:
            fcntl.ioctl(self._fd, C.FE_GET_PROPERTY, header)
        except OSError as e:
            raise FrontendError(
                e.errno, f"FE_GET_PROPERTY on {self.path}: {e.strerror}", self.path
            ) from e
        return {props_array[i].cmd: props_array[i] for i in range(n)}

    def read_stats(self) -> FrontendStats:
        """Lectura completa para el monitor: lock + signal + CNR + pre-BER."""
        lock = self.read_status()
        props = self.read_properties(
            C.DTV_STAT_SIGNAL_STRENGTH,
            C.DTV_STAT_CNR,
            C.DTV_STAT_PRE_ERROR_BIT_COUNT,
            C.DTV_STAT_PRE_TOTAL_BIT_COUNT,
        )
        signal = _extract_stat_samples(props[C.DTV_STAT_SIGNAL_STRENGTH])
        cnr = _extract_stat_samples(props[C.DTV_STAT_CNR])
        pre_err = _first_sample_or_none(props[C.DTV_STAT_PRE_ERROR_BIT_COUNT])
        pre_tot = _first_sample_or_none(props[C.DTV_STAT_PRE_TOTAL_BIT_COUNT])
        return FrontendStats(
            lock=lock,
            signal_strength=signal,
            cnr=cnr,
            pre_ber_error=pre_err,
            pre_ber_total=pre_tot,
        )

    # --- escrituras (tune / clear) ---

    def tune(self, frequency_hz: int, delivery_system: int, modulation: int) -> None:
        """Sintoniza el frontend con FE_SET_PROPERTY.

        Setea delivery_system, frequency, modulation y dispara DTV_TUNE en una sola
        llamada. La lock state es asíncrona — el demod va a tardar 100-1500 ms
        en mostrar lock vía FE_READ_STATUS según señal y tipo.
        """
        if self.read_only:
            raise FrontendError(errno.EACCES, f"Frontend {self.path} opened read-only", self.path)
        # DTV_CLEAR resetea cualquier tune previo antes de set
        cmds: list[tuple[int, int]] = [
            (C.DTV_CLEAR, 0),
            (C.DTV_DELIVERY_SYSTEM, delivery_system),
            (C.DTV_FREQUENCY, frequency_hz),
            (C.DTV_MODULATION, modulation),
            (C.DTV_TUNE, 0),
        ]
        self._set_properties_simple(cmds)

    def clear(self) -> None:
        """Libera el tune (DTV_CLEAR). El frontend queda en idle."""
        if self.read_only:
            raise FrontendError(errno.EACCES, f"Frontend {self.path} opened read-only", self.path)
        self._set_properties_simple([(C.DTV_CLEAR, 0)])

    def _set_properties_simple(self, cmds: list[tuple[int, int]]) -> None:
        """FE_SET_PROPERTY con valores simples u.data — no soporta stats ni buffers."""
        self._require_open()
        n = len(cmds)
        props_array = (C.DtvProperty * n)()
        for i, (cmd, value) in enumerate(cmds):
            props_array[i].cmd = cmd
            props_array[i].u.data = value
        header = C.DtvProperties()
        header.num = n
        header.props = ctypes.cast(props_array, ctypes.POINTER(C.DtvProperty))
        try:
            fcntl.ioctl(self._fd, C.FE_SET_PROPERTY, header)
        except OSError as e:
            raise FrontendError(
                e.errno, f"FE_SET_PROPERTY on {self.path}: {e.strerror}", self.path
            ) from e

    # --- más lecturas ---

    def read_tune_info(self) -> TuneInfo:
        """FE_GET_PROPERTY de DTV_FREQUENCY/DELIVERY_SYSTEM/MODULATION.

        Si nadie llamó FE_SET_PROPERTY previamente (frontend recién abierto, no
        tuneado), los valores son los defaults del driver — frecuencia 0.
        """
        props = self.read_properties(C.DTV_FREQUENCY, C.DTV_DELIVERY_SYSTEM, C.DTV_MODULATION)
        return TuneInfo(
            frequency_hz=props[C.DTV_FREQUENCY].u.data,
            delivery_system=props[C.DTV_DELIVERY_SYSTEM].u.data,
            modulation=props[C.DTV_MODULATION].u.data,
        )

    # --- internals ---

    def _require_open(self) -> None:
        if self._fd is None:
            raise FrontendError(errno.EBADF, f"Frontend {self.path} not open", self.path)


def _extract_stat_samples(prop: C.DtvProperty) -> list[StatSample]:
    """Lee los `len` samples de un DTV_STAT_* DtvProperty.u.st."""
    n = prop.u.st.len
    return [StatSample(scale=prop.u.st.stat[i].scale, value=prop.u.st.stat[i].value) for i in range(n)]


def _first_sample_or_none(prop: C.DtvProperty) -> StatSample | None:
    samples = _extract_stat_samples(prop)
    return samples[0] if samples else None
