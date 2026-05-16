"""Wrapper del demux DVB para capturar el TS completo del adapter sintonizado.

Uso típico (post-tune):

    with Demux(adapter=0) as dmx:
        dmx.set_filter_all_pids(buffer_kb=512)
        with open(f"/dev/dvb/adapter0/dvr0", "rb") as dvr:
            while True:
                chunk = dvr.read(188 * 200)
                ...  # mandar al cliente

Sin set_filter_*, `dvr0` queda mudo (no entrega bytes).
"""

from __future__ import annotations

import ctypes
import errno
import fcntl
import os
from dataclasses import dataclass

from sintonizador.dvb import constants as C


# --- struct dmx_pes_filter_params (linux/dvb/dmx.h, sizeof=20 en x86_64) ---


class DmxPesFilterParams(ctypes.Structure):
    _fields_ = [
        ("pid", ctypes.c_uint16),
        ("_pad", ctypes.c_uint16),  # padding natural antes del int (offset 4)
        ("input", ctypes.c_int32),
        ("output", ctypes.c_int32),
        ("pes_type", ctypes.c_int32),
        ("flags", ctypes.c_uint32),
    ]


assert ctypes.sizeof(DmxPesFilterParams) == 20, ctypes.sizeof(DmxPesFilterParams)


# --- enums del kernel (linux/dvb/dmx.h) ---

DMX_IN_FRONTEND = 0
DMX_OUT_DECODER = 0
DMX_OUT_TAP = 1
DMX_OUT_TS_TAP = 2  # ruteo del filter hacia dvr0 — lo que queremos
DMX_OUT_TSDEMUX_TAP = 3

# DMX_PES_OTHER = 20: tipo "no me importa", apto para pass-through con PID 0x2000
DMX_PES_OTHER = 20

# flags
DMX_CHECK_CRC = 0x01
DMX_ONESHOT = 0x02
DMX_IMMEDIATE_START = 0x04  # arrancar el filtro al setearlo (sin DMX_START extra)

# PID = 0x2000: "todos los PIDs" (convención del kernel DVB)
DMX_PID_ANY = 0x2000


# --- ioctls DMX (type 'o', mismos que el frontend) ---


def _IO(type_: int, nr: int) -> int:
    return C._IOC(C._IOC_NONE, type_, nr, 0)


DMX_TYPE = ord("o")

DMX_SET_PES_FILTER = C._IOW(DMX_TYPE, 44, ctypes.sizeof(DmxPesFilterParams))
DMX_SET_BUFFER_SIZE = _IO(DMX_TYPE, 45)  # toma unsigned long directo como arg
DMX_START = _IO(DMX_TYPE, 41)
DMX_STOP = _IO(DMX_TYPE, 42)


class DemuxError(OSError):
    """Error específico del demux."""


@dataclass
class Demux:
    """Maneja un `/dev/dvb/adapter{N}/demux0` para configurar el TS tap.

    NO lee bytes del TS — eso se hace abriendo `dvr0` por separado. Este wrapper
    solo configura el filtro que hace que `dvr0` empiece a entregar datos.

    El fd del demux debe mantenerse abierto mientras dure la sesión de
    streaming; cerrarlo aborta la entrega al dvr0.
    """

    adapter: int
    demux: int = 0
    _fd: int | None = None

    @property
    def path(self) -> str:
        return f"/dev/dvb/adapter{self.adapter}/demux{self.demux}"

    @property
    def dvr_path(self) -> str:
        return f"/dev/dvb/adapter{self.adapter}/dvr0"

    def open(self) -> None:
        if self._fd is not None:
            return
        try:
            self._fd = os.open(self.path, os.O_RDWR)
        except OSError as e:
            raise DemuxError(e.errno, f"opening {self.path}: {e.strerror}", self.path) from e

    def close(self) -> None:
        if self._fd is None:
            return
        try:
            os.close(self._fd)
        finally:
            self._fd = None

    def __enter__(self) -> Demux:
        self.open()
        return self

    def __exit__(self, *_exc: object) -> None:
        self.close()

    def set_buffer_size(self, size_bytes: int) -> None:
        """DMX_SET_BUFFER_SIZE — agranda el buffer del kernel para evitar drops."""
        self._require_open()
        try:
            fcntl.ioctl(self._fd, DMX_SET_BUFFER_SIZE, size_bytes)
        except OSError as e:
            raise DemuxError(
                e.errno, f"DMX_SET_BUFFER_SIZE({size_bytes}) on {self.path}: {e.strerror}", self.path
            ) from e

    def set_filter_all_pids(self, buffer_kb: int = 512) -> None:
        """Configura el demux para enviar TODO el TS (todos los PIDs) al dvr0.

        Después: leer bytes de `/dev/dvb/adapter{N}/dvr0` entrega el TS en vivo.
        Solo UN consumer puede leer dvr0 a la vez (bytes se reparten entre lectores).
        Para múltiples consumers concurrentes usar `set_filter_all_pids_tsdemux_tap`.
        """
        self._set_filter(output=DMX_OUT_TS_TAP, buffer_kb=buffer_kb)

    def set_filter_all_pids_tsdemux_tap(self, buffer_kb: int = 512) -> None:
        """Configura el demux para entregar TS por el FD del propio demux.

        A diferencia de `set_filter_all_pids` (que rutea a dvr0), acá los bytes
        salen directo del fd `self._fd` con `os.read()`. Permite tener un
        info-extractor en paralelo a otro consumer que esté leyendo dvr0.
        """
        self._set_filter(output=DMX_OUT_TSDEMUX_TAP, buffer_kb=buffer_kb)

    def _set_filter(self, output: int, buffer_kb: int) -> None:
        self._require_open()
        if buffer_kb > 0:
            self.set_buffer_size(buffer_kb * 1024)
        params = DmxPesFilterParams()
        params.pid = DMX_PID_ANY
        params.input = DMX_IN_FRONTEND
        params.output = output
        params.pes_type = DMX_PES_OTHER
        params.flags = DMX_IMMEDIATE_START
        try:
            fcntl.ioctl(self._fd, DMX_SET_PES_FILTER, params)
        except OSError as e:
            raise DemuxError(
                e.errno, f"DMX_SET_PES_FILTER(output={output}) on {self.path}: {e.strerror}", self.path
            ) from e

    @property
    def fd(self) -> int:
        """Fd subyacente — útil cuando se usó TSDEMUX_TAP para leer directo del demux."""
        self._require_open()
        return self._fd  # type: ignore[return-value]

    def _require_open(self) -> None:
        if self._fd is None:
            raise DemuxError(errno.EBADF, f"Demux {self.path} not open", self.path)
