"""Poller asyncio que muestrea los 4 tuners cada N ms y emite snapshots.

- Mantiene un `Frontend` abierto por adapter (read-only).
- Lee `read_tune_info` + `read_stats` por iteración.
- Aplica filtro del lgdt3306a: descarta C/N == 1.29 dB exacto (valor centinela).
- Mantiene el último snapshot accesible (`current()`) + canal pubsub para WS
  (`subscribe()` → AsyncIterator[list[TunerSnapshot]]).
"""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass, field
from typing import AsyncIterator

from sintonizador.dvb import constants as C
from sintonizador.dvb.frontend import Frontend, FrontendError, FrontendStats, TuneInfo

log = logging.getLogger(__name__)


# Valor centinela del driver lgdt3306a: cuando aparece esto post-lock no es C/N
# real — el demod no actualizó el registro. Confirmado empíricamente 2026-05-13.
LGDT3306A_CNR_SENTINEL_MDB = 1290  # 1.29 dB en milli-dB


@dataclass(slots=True)
class TunerSnapshot:
    """Estado de un tuner en un instante. Lo que sale por WS."""

    adapter: int
    timestamp: float  # epoch seconds
    available: bool  # False si el frontend no se pudo abrir/leer

    # Tune actual (None si el adapter está libre o no se pudo leer)
    frequency_hz: int | None = None
    delivery_system: str | None = None
    modulation: str | None = None

    # Lock state
    has_signal: bool = False
    has_carrier: bool = False
    has_lock: bool = False
    lock_raw: int = 0

    # Métricas (None cuando no aplica)
    signal_dbm: float | None = None  # FE_STAT_SIGNAL_STRENGTH en escala DECIBEL
    cnr_db: float | None = None  # FE_STAT_CNR, con filtro de sentinel aplicado
    cnr_sentinel: bool = False  # True si la última lectura cayó en el sentinel

    # Diagnóstico
    error: str | None = None


@dataclass
class _AdapterRunner:
    """Estado interno por adapter."""

    adapter: int
    frontend: Frontend
    last_snapshot: TunerSnapshot | None = None
    consecutive_errors: int = 0


@dataclass
class _Subscription:
    queue: asyncio.Queue[list[TunerSnapshot]] = field(default_factory=asyncio.Queue)


class MonitorPoller:
    """Muestrea los 4 tuners cada `interval_s` y publica a suscriptores.

    Uso:

        poller = MonitorPoller(adapters=[0, 1, 2, 3], interval_s=0.5)
        await poller.start()
        snapshot = poller.current()
        async for update in poller.subscribe():
            ...
        await poller.stop()
    """

    def __init__(self, adapters: list[int], interval_s: float = 0.5) -> None:
        self.adapters = adapters
        self.interval_s = interval_s
        self._runners: dict[int, _AdapterRunner] = {}
        self._subs: list[_Subscription] = []
        self._task: asyncio.Task[None] | None = None
        self._stopping = asyncio.Event()
        # Adapters "reservados" — tipicamente por el archiver. La API de tune
        # rechaza tune/clear sobre estos adapters con 409.
        self._reserved: set[int] = set()

    async def start(self) -> None:
        if self._task is not None:
            return
        for adapter in self.adapters:
            fe = Frontend(adapter=adapter)
            try:
                fe.open()
            except FrontendError as e:
                log.warning("adapter %d: cannot open frontend (%s) — will retry", adapter, e)
            self._runners[adapter] = _AdapterRunner(adapter=adapter, frontend=fe)
        self._task = asyncio.create_task(self._run(), name="monitor-poller")

    async def stop(self) -> None:
        self._stopping.set()
        if self._task is not None:
            await self._task
            self._task = None
        for runner in self._runners.values():
            runner.frontend.close()
        self._runners.clear()

    def current(self) -> list[TunerSnapshot]:
        """Snapshot estable de los 4 tuners (lo último visto)."""
        return [r.last_snapshot for r in self._runners.values() if r.last_snapshot is not None]

    def tune(self, adapter: int, frequency_hz: int, delivery_system: int, modulation: int) -> None:
        """Sintoniza el adapter dado.

        Reutiliza el `Frontend` ya abierto por el poller (RDWR). No bloquea
        esperando lock — la siguiente iteración del poll va a reflejarlo.
        """
        runner = self._require_runner(adapter)
        if not runner.frontend.is_open:
            runner.frontend.open()
        runner.frontend.tune(frequency_hz, delivery_system, modulation)

    def clear(self, adapter: int) -> None:
        """Libera el tune del adapter (DTV_CLEAR)."""
        runner = self._require_runner(adapter)
        if not runner.frontend.is_open:
            runner.frontend.open()
        runner.frontend.clear()

    def _require_runner(self, adapter: int) -> _AdapterRunner:
        runner = self._runners.get(adapter)
        if runner is None:
            raise KeyError(f"adapter {adapter} no está gestionado por el poller (managed: {sorted(self._runners)})")
        return runner

    # --- reserva (uso del archiver) ---

    def reserve(self, adapter: int) -> None:
        """Marca un adapter como reservado. La API de tune lo rechaza con 409."""
        self._reserved.add(adapter)

    def release(self, adapter: int) -> None:
        """Libera la reserva del adapter."""
        self._reserved.discard(adapter)

    def is_reserved(self, adapter: int) -> bool:
        return adapter in self._reserved

    @property
    def reserved_adapters(self) -> set[int]:
        return set(self._reserved)

    def subscribe(self) -> AsyncIterator[list[TunerSnapshot]]:
        """AsyncIterator que recibe cada batch de updates. Una cola por subscriptor."""
        sub = _Subscription()
        self._subs.append(sub)

        async def _gen() -> AsyncIterator[list[TunerSnapshot]]:
            try:
                while True:
                    batch = await sub.queue.get()
                    yield batch
            finally:
                if sub in self._subs:
                    self._subs.remove(sub)

        return _gen()

    # --- internals ---

    async def _run(self) -> None:
        log.info("monitor poller started: adapters=%s interval=%.2fs", self.adapters, self.interval_s)
        while not self._stopping.is_set():
            t0 = time.monotonic()
            batch = [self._poll_one(runner) for runner in self._runners.values()]
            await self._broadcast(batch)
            elapsed = time.monotonic() - t0
            try:
                await asyncio.wait_for(self._stopping.wait(), timeout=max(0.0, self.interval_s - elapsed))
            except asyncio.TimeoutError:
                pass
        log.info("monitor poller stopped")

    def _poll_one(self, runner: _AdapterRunner) -> TunerSnapshot:
        snap = TunerSnapshot(adapter=runner.adapter, timestamp=time.time(), available=False)
        if not runner.frontend.is_open:
            # Intentamos reabrir cada vez — si otra app lo tenía exclusivo y la cerró,
            # podemos volver a leer.
            try:
                runner.frontend.open()
            except FrontendError as e:
                snap.error = str(e)
                runner.last_snapshot = snap
                return snap
        try:
            tune = runner.frontend.read_tune_info()
            stats = runner.frontend.read_stats()
            self._fill_snapshot(snap, tune, stats)
            runner.consecutive_errors = 0
        except FrontendError as e:
            snap.error = str(e)
            runner.consecutive_errors += 1
            # Si fallan varias seguidas, cerrar para que el próximo open intente fresh
            if runner.consecutive_errors >= 3:
                runner.frontend.close()
        runner.last_snapshot = snap
        return snap

    def _fill_snapshot(self, snap: TunerSnapshot, tune: TuneInfo, stats: FrontendStats) -> None:
        snap.available = True
        # Si no hay tune previo (freq=0 tras boot o tras clear()), el kernel
        # puede reportar lock bits y signal stale de un tune anterior. Tratamos
        # todo eso como "sin sintonizar" para no engañar a la UI.
        if not tune.frequency_hz:
            snap.frequency_hz = None
            snap.delivery_system = None
            snap.modulation = None
            snap.lock_raw = 0
            return
        snap.frequency_hz = tune.frequency_hz
        snap.delivery_system = C.DELIVERY_SYSTEM_NAMES.get(tune.delivery_system, str(tune.delivery_system))
        snap.modulation = C.MODULATION_NAMES.get(tune.modulation, str(tune.modulation))
        snap.has_signal = stats.lock.has_signal
        snap.has_carrier = stats.lock.has_carrier
        snap.has_lock = stats.lock.has_lock
        snap.lock_raw = stats.lock.raw
        # Signal: tomar primer sample con scale=DECIBEL (dBm)
        snap.signal_dbm = _pick_decibel(stats.signal_strength)
        # CNR: con filtro del sentinel del lgdt3306a
        snap.cnr_db, snap.cnr_sentinel = _pick_cnr_with_filter(stats.cnr)

    async def _broadcast(self, batch: list[TunerSnapshot]) -> None:
        for sub in list(self._subs):
            try:
                # No bloquear el poller: si el consumidor es lento, descartar el oldest
                if sub.queue.full():
                    try:
                        sub.queue.get_nowait()
                    except asyncio.QueueEmpty:
                        pass
                sub.queue.put_nowait(batch)
            except Exception:
                log.exception("error broadcasting to subscriber; removing")
                if sub in self._subs:
                    self._subs.remove(sub)


def _pick_decibel(samples: list) -> float | None:  # type: ignore[type-arg]
    """Primer sample con scale=DECIBEL convertido a unidades reales (dB / dBm)."""
    for s in samples:
        if s.scale == C.FE_SCALE_DECIBEL:
            return s.db
    return None


def _pick_cnr_with_filter(samples: list) -> tuple[float | None, bool]:  # type: ignore[type-arg]
    """C/N en dB con filtro: descarta el valor centinela 1.29 dB del lgdt3306a.

    Devuelve (cnr_db, was_sentinel).
    """
    for s in samples:
        if s.scale != C.FE_SCALE_DECIBEL:
            continue
        if s.value == LGDT3306A_CNR_SENTINEL_MDB:
            return (None, True)
        return (s.db, False)
    return (None, False)
