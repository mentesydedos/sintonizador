"""Wrapper Python sobre DVB API v5 (ioctls + ctypes)."""

from sintonizador.dvb.adapters import detect_adapters
from sintonizador.dvb.frontend import Frontend, FrontendStats, LockState, StatSample, TuneInfo
from sintonizador.dvb.constants import (
    DTV_STAT_SIGNAL_STRENGTH,
    DTV_STAT_CNR,
    DTV_STAT_PRE_ERROR_BIT_COUNT,
    DTV_STAT_PRE_TOTAL_BIT_COUNT,
    FE_SCALE_DECIBEL,
    FE_SCALE_RELATIVE,
    FE_SCALE_COUNTER,
)

__all__ = [
    "detect_adapters",
    "Frontend",
    "FrontendStats",
    "LockState",
    "StatSample",
    "TuneInfo",
    "DTV_STAT_SIGNAL_STRENGTH",
    "DTV_STAT_CNR",
    "DTV_STAT_PRE_ERROR_BIT_COUNT",
    "DTV_STAT_PRE_TOTAL_BIT_COUNT",
    "FE_SCALE_DECIBEL",
    "FE_SCALE_RELATIVE",
    "FE_SCALE_COUNTER",
]
