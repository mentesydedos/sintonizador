"""`MonitorService`: el usuario elige hasta 16 subcanales → se calculan los
muxes (≤ nº de tuners), se asignan a tuners y se sintonizan.

Constraints de diseño:
- Hasta 16 subcanales viven en ≤8 muxes; un tuner = un mux. Si la selección
  pide más muxes que tuners disponibles → error 400.
- Regla anti-doble-tap: si un mux YA está sintonizado en algún adapter (por el
  archiver 24×7 o por una selección previa), se REUSA ese adapter en vez de
  re-tunear / abrir otro tap.
- No se toca el archiver: si un mux coincide con uno del archiver, el monitoreo
  cuelga sus consumidores del mismo MuxReader compartido.

CC y video de los subcanales seleccionados los manejan el `SubchannelCCManager`
y el `TranscodeManager` (ref-contados, on-demand desde el frontend).
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

from sintonizador.archiver.config import make_slug

if TYPE_CHECKING:
    from sintonizador.channels import Channel
    from sintonizador.monitor import MonitorPoller

log = logging.getLogger(__name__)

MAX_SUBCHANNELS = 16


def assign_muxes_to_adapters(
    muxes: list[int],
    all_adapters: list[int],
    tuned_map: dict[int, int],
    reserved: set[int],
    unavailable: set[int],
) -> dict[int, int]:
    """Asigna cada frecuencia (mux) a un adapter.

    - `tuned_map`: freq → adapter ya sintonizado a esa freq (se reusa).
    - `reserved`: adapters reservados por el archiver (no se pisan, salvo reuse).
    - `unavailable`: adapters sin hardware/abrir (se excluyen).
    Devuelve freq → adapter. Tira ValueError si no alcanzan los tuners.
    """
    assignment: dict[int, int] = {}
    used: set[int] = set()
    # 1) Reusar muxes ya sintonizados (incluye los del archiver)
    for freq in muxes:
        if freq in tuned_map:
            adapter = tuned_map[freq]
            assignment[freq] = adapter
            used.add(adapter)
    # 2) Asignar adapters libres a los muxes restantes
    remaining = [f for f in muxes if f not in assignment]
    avail = [
        a for a in all_adapters
        if a not in used and a not in reserved and a not in unavailable
    ]
    if len(remaining) > len(avail):
        total = len(used) + len(avail)
        raise ValueError(
            f"la selección necesita {len(muxes)} muxes (tuners) pero solo hay "
            f"{total} disponibles ({len(avail)} libres + {len(used)} ya en uso)"
        )
    for freq, adapter in zip(remaining, avail):
        assignment[freq] = adapter
        used.add(adapter)
    return assignment


class MonitorService:
    def __init__(self, poller: "MonitorPoller", channels: list["Channel"]) -> None:
        self.poller = poller
        self.channels = channels
        self.selection: list[int] = []
        self.owned_adapters: set[int] = set()  # adapters que tuneó el monitor

    def refresh_channels(self, channels: list["Channel"]) -> None:
        self.channels = channels

    def owns(self, adapter: int) -> bool:
        return adapter in self.owned_adapters

    def _resolve(self, channel_ids: list[int]) -> list[tuple[int, "Channel"]]:
        sel: list[tuple[int, "Channel"]] = []
        for cid in channel_ids:
            if not (0 <= cid < len(self.channels)):
                raise ValueError(f"channel_id {cid} fuera de rango")
            sel.append((cid, self.channels[cid]))
        return sel

    def plan(self, channel_ids: list[int]) -> dict[str, Any]:
        """Calcula el plan (muxes + asignación) sin tocar el hardware."""
        if not channel_ids:
            raise ValueError("selección vacía")
        if len(channel_ids) > MAX_SUBCHANNELS:
            raise ValueError(f"máximo {MAX_SUBCHANNELS} subcanales (pediste {len(channel_ids)})")
        sel = self._resolve(channel_ids)
        # Muxes (frecuencias) distintos, en orden de aparición
        muxes: list[int] = []
        for _cid, ch in sel:
            if ch.frequency_hz not in muxes:
                muxes.append(ch.frequency_hz)

        snaps = {s.adapter: s for s in self.poller.current()}
        tuned_map: dict[int, int] = {}
        for a, s in snaps.items():
            if s.frequency_hz:
                tuned_map.setdefault(s.frequency_hz, a)
        reserved = self.poller.reserved_adapters
        unavailable = {a for a, s in snaps.items() if not s.available}

        assignment = assign_muxes_to_adapters(
            muxes, list(self.poller.adapters), tuned_map, reserved, unavailable
        )

        # Armar vista por mux con sus subcanales seleccionados
        by_mux: dict[int, list[dict]] = {f: [] for f in muxes}
        for cid, ch in sel:
            by_mux[ch.frequency_hz].append({
                "channel_id": cid,
                "slug": make_slug(ch.vchannel, ch.name),
                "vchannel": ch.vchannel,
                "name": ch.name,
                "service_id": ch.service_id,
            })
        plan = {
            "selection": channel_ids,
            "muxes": [
                {
                    "frequency_hz": f,
                    "frequency_mhz": round(f / 1e6, 3),
                    "adapter": assignment[f],
                    "reused": f in tuned_map and tuned_map[f] == assignment[f],
                    "subchannels": by_mux[f],
                }
                for f in muxes
            ],
            "tuners_needed": len(muxes),
            "tuners_total": len(self.poller.adapters),
        }
        return plan

    def set_selection(self, channel_ids: list[int]) -> dict[str, Any]:
        """Aplica el plan: tunea los muxes que falten y registra la selección."""
        plan = self.plan(channel_ids)
        sel = self._resolve(channel_ids)
        ch_by_freq = {ch.frequency_hz: ch for _cid, ch in sel}
        snaps = {s.adapter: s for s in self.poller.current()}

        newly_owned: set[int] = set()
        for mux in plan["muxes"]:
            freq = mux["frequency_hz"]
            adapter = mux["adapter"]
            snap = snaps.get(adapter)
            already = snap is not None and snap.frequency_hz == freq
            reserved = self.poller.is_reserved(adapter)
            if already or reserved:
                # Reuso (archiver o tune previo) — no re-tunear, no marcar owned
                continue
            ch = ch_by_freq[freq]
            self.poller.tune(adapter, freq, ch.delivery_system, ch.modulation)
            newly_owned.add(adapter)
            log.info("monitor: tuned adapter %d → %.3f MHz", adapter, freq / 1e6)

        self.owned_adapters |= newly_owned
        self.selection = list(channel_ids)
        plan["owned_adapters"] = sorted(self.owned_adapters)
        return plan

    def status(self) -> dict[str, Any]:
        if not self.selection:
            return {"active": False, "selection": [], "muxes": [], "owned_adapters": []}
        try:
            plan = self.plan(self.selection)
        except ValueError as e:
            return {"active": True, "selection": self.selection, "error": str(e),
                    "owned_adapters": sorted(self.owned_adapters)}
        plan["active"] = True
        plan["owned_adapters"] = sorted(self.owned_adapters)
        return plan

    async def teardown(self) -> None:
        """Libera los tuners que el monitor tuneó (no toca los del archiver)."""
        for adapter in sorted(self.owned_adapters):
            try:
                self.poller.clear(adapter)
                log.info("monitor: liberado adapter %d", adapter)
            except Exception:
                log.exception("monitor: clear adapter %d falló", adapter)
        self.owned_adapters.clear()
        self.selection = []
