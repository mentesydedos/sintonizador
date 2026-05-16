"""Constantes del DVB API v5: números de ioctl, command IDs, bitmasks de estado."""

import ctypes

# --- ioctl encoding (Linux _IOC macro family) ---
_IOC_NRBITS = 8
_IOC_TYPEBITS = 8
_IOC_SIZEBITS = 14
_IOC_NRSHIFT = 0
_IOC_TYPESHIFT = _IOC_NRSHIFT + _IOC_NRBITS
_IOC_SIZESHIFT = _IOC_TYPESHIFT + _IOC_TYPEBITS
_IOC_DIRSHIFT = _IOC_SIZESHIFT + _IOC_SIZEBITS

_IOC_NONE = 0
_IOC_WRITE = 1
_IOC_READ = 2


def _IOC(direction: int, type_: int, nr: int, size: int) -> int:
    return (
        (direction << _IOC_DIRSHIFT)
        | (type_ << _IOC_TYPESHIFT)
        | (nr << _IOC_NRSHIFT)
        | (size << _IOC_SIZESHIFT)
    )


def _IOR(type_: int, nr: int, size: int) -> int:
    return _IOC(_IOC_READ, type_, nr, size)


def _IOW(type_: int, nr: int, size: int) -> int:
    return _IOC(_IOC_WRITE, type_, nr, size)


# --- DVB ioctl type byte ---
DVB_IOCTL_TYPE = ord("o")

# --- fe_status_t is a 4-byte enum ---
FE_READ_STATUS = _IOR(DVB_IOCTL_TYPE, 69, 4)

# --- FE_HAS_* bitmask ---
FE_HAS_SIGNAL = 0x01
FE_HAS_CARRIER = 0x02
FE_HAS_VITERBI = 0x04
FE_HAS_SYNC = 0x08
FE_HAS_LOCK = 0x10
FE_TIMEDOUT = 0x20
FE_REINIT = 0x40

# --- DVB API v5 DTV command IDs (linux/dvb/frontend.h) ---
DTV_UNDEFINED = 0
DTV_TUNE = 1
DTV_CLEAR = 2
DTV_FREQUENCY = 3
DTV_MODULATION = 4
DTV_BANDWIDTH_HZ = 5
DTV_INVERSION = 6
DTV_DELIVERY_SYSTEM = 17
DTV_API_VERSION = 35
DTV_ENUM_DELSYS = 44

DTV_STAT_SIGNAL_STRENGTH = 62
DTV_STAT_CNR = 63
DTV_STAT_PRE_ERROR_BIT_COUNT = 64
DTV_STAT_PRE_TOTAL_BIT_COUNT = 65
DTV_STAT_POST_ERROR_BIT_COUNT = 66
DTV_STAT_POST_TOTAL_BIT_COUNT = 67
DTV_STAT_ERROR_BLOCK_COUNT = 68
DTV_STAT_TOTAL_BLOCK_COUNT = 69

# --- FE_SCALE_* — significado del campo `scale` en dtv_fe_stats ---
FE_SCALE_NOT_AVAILABLE = 0
FE_SCALE_DECIBEL = 1  # value en milli-(unidad), p.ej. mdB o mdBm — dividir por 1000
FE_SCALE_RELATIVE = 2  # value en 0..65535 (porcentual)
FE_SCALE_COUNTER = 3  # value es un contador acumulativo (bits, blocks)


# --- Tamaño máximo del array stat[] dentro de dtv_fe_stats ---
MAX_DTV_STATS = 4


# --- DTV_DELIVERY_SYSTEM enum (subset usado) ---
SYS_UNDEFINED = 0
SYS_DVBC_ANNEX_A = 1
SYS_DVBC_ANNEX_B = 2  # cable americano (Clear-QAM, lo que también puede tu TBS6704)
SYS_DVBT = 3
SYS_DVBS = 5
SYS_DVBS2 = 6
SYS_ATSC = 11
SYS_DVBC_ANNEX_C = 18

DELIVERY_SYSTEM_NAMES = {
    SYS_UNDEFINED: "UNDEFINED",
    SYS_DVBC_ANNEX_A: "DVB-C/A",
    SYS_DVBC_ANNEX_B: "DVB-C/B (Clear-QAM US)",
    SYS_DVBT: "DVB-T",
    SYS_DVBS: "DVB-S",
    SYS_DVBS2: "DVB-S2",
    SYS_ATSC: "ATSC",
    SYS_DVBC_ANNEX_C: "DVB-C/C",
}

# --- DTV_MODULATION enum (subset usado) ---
QPSK = 0
QAM_16 = 1
QAM_32 = 2
QAM_64 = 3
QAM_128 = 4
QAM_256 = 5
QAM_AUTO = 6
VSB_8 = 7  # ATSC OTA terrestre
VSB_16 = 8

MODULATION_NAMES = {
    QPSK: "QPSK",
    QAM_16: "QAM-16",
    QAM_32: "QAM-32",
    QAM_64: "QAM-64",
    QAM_128: "QAM-128",
    QAM_256: "QAM-256",
    QAM_AUTO: "QAM-AUTO",
    VSB_8: "8-VSB",
    VSB_16: "16-VSB",
}


# --- ctypes Structures que mapean linux/dvb/frontend.h ---


class DtvFeStat(ctypes.Structure):
    """struct dtv_stats — { __u8 scale; union { __u64 uvalue; __s64 svalue; }; } __packed.

    Importante: `scale` es __u8 en el header del kernel, no __u32. Total packed = 9 bytes.
    """

    _pack_ = 1
    _fields_ = [
        ("scale", ctypes.c_uint8),
        ("value", ctypes.c_int64),  # uvalue/svalue union — leemos como signed
    ]


# 1 (scale) + 8 (value) packed = 9 bytes por entry
assert ctypes.sizeof(DtvFeStat) == 9, ctypes.sizeof(DtvFeStat)


class DtvFeStats(ctypes.Structure):
    """struct { __u8 len; dtv_stats stat[MAX_DTV_STATS]; } __packed."""

    _pack_ = 1
    _fields_ = [
        ("len", ctypes.c_uint8),
        ("stat", DtvFeStat * MAX_DTV_STATS),
    ]


# 1 (len) + 4*9 (stats) = 37 bytes — coincide con sizeof(struct dtv_fe_stats) del kernel
assert ctypes.sizeof(DtvFeStats) == 37, ctypes.sizeof(DtvFeStats)


class DtvPropertyBuffer(ctypes.Structure):
    """struct { __u8 data[32]; __u32 len; __u32 reserved1[3]; void *reserved2; } __packed."""

    _pack_ = 1
    _fields_ = [
        ("data", ctypes.c_uint8 * 32),
        ("len", ctypes.c_uint32),
        ("reserved1", ctypes.c_uint32 * 3),
        ("reserved2", ctypes.c_void_p),
    ]


class DtvPropertyUnion(ctypes.Union):
    """union { __u32 data; struct dtv_fe_stats st; struct{...} buffer; }."""

    _pack_ = 1
    _fields_ = [
        ("data", ctypes.c_uint32),
        ("st", DtvFeStats),
        ("buffer", DtvPropertyBuffer),
    ]


class DtvProperty(ctypes.Structure):
    """struct dtv_property — un comando + sus datos.

    __packed en el kernel.
    """

    _pack_ = 1
    _fields_ = [
        ("cmd", ctypes.c_uint32),
        ("reserved", ctypes.c_uint32 * 3),
        ("u", DtvPropertyUnion),
        ("result", ctypes.c_int32),
    ]


class DtvProperties(ctypes.Structure):
    """struct dtv_properties — header del array de properties.

    NO está packed en el header del kernel; tiene padding natural en x86_64.
    """

    _fields_ = [
        ("num", ctypes.c_uint32),
        ("props", ctypes.POINTER(DtvProperty)),
    ]


# tamaño esperado en x86_64: 4 (num) + 4 (padding) + 8 (ptr) = 16
assert ctypes.sizeof(DtvProperties) == 16, ctypes.sizeof(DtvProperties)

# Y ahora podemos definir los ioctls FE_GET/SET_PROPERTY usando el tamaño correcto.
# Ambos toman struct dtv_properties (16 bytes en x86_64). El kernel diferencia
# read vs write por el bit de dirección del ioctl number.
FE_GET_PROPERTY = _IOR(DVB_IOCTL_TYPE, 83, ctypes.sizeof(DtvProperties))
FE_SET_PROPERTY = _IOW(DVB_IOCTL_TYPE, 82, ctypes.sizeof(DtvProperties))
