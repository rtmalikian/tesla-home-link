"""Live fleet snapshot: no-wake reads of each configured vehicle, distilled
into readings the home rules engine, LLM context builder, and the AI chat
surface share.

The fields that matter here: location, asleep state, active navigation
destination + minutes-to-arrival (the primary approach signal), and the
last-known indoor temperature / porch-light state, which are injected from
the home backends.
"""
from __future__ import annotations

from dataclasses import dataclass, field

from .home import HomeReading


@dataclass
class FleetSnapshot:
    readings: list[HomeReading] = field(default_factory=list)
    unreachable: dict = field(default_factory=dict)  # vin -> error text

    def brief(self) -> str:
        """Compact text the LLM receives as context."""
        lines = []
        for r in self.readings:
            state = "asleep" if r.asleep else "awake"
            nav = f", navigating to {r.nav_dest} ({r.nav_eta_min:.0f} min out)" \
                if r.nav_dest else ", not navigating"
            indoor = (f", indoor {r.indoor_temp_f:g}°F"
                      if r.indoor_temp_f is not None else "")
            lines.append(f"- {r.name}: {state}{nav}{indoor}")
        for vin, err in self.unreachable.items():
            lines.append(f"- {vin}: unreachable ({err})")
        return "\n".join(lines) or "(no vehicles)"


def _drive(d: dict) -> dict:
    return d.get("drive_state") or {}


def snapshot_fleet(client, vehicles: list) -> FleetSnapshot:
    """Read every configured vehicle (no-wake reads only) and build a snapshot."""
    snap = FleetSnapshot()
    for vc in vehicles:
        try:
            data = client.vehicle_data(vc.vin)
        except Exception as exc:  # never let one car break the whole fleet view
            snap.unreachable[vc.vin] = str(exc)[:120]
            continue
        dr = _drive(data)
        asleep = str(data.get("state") or "").lower() == "asleep"
        lat = dr.get("latitude")
        lon = dr.get("longitude")
        nav_dest = dr.get("active_route_destination")
        nav_eta = dr.get("active_route_minutes_to_arrival")
        try:
            nav_eta = float(nav_eta) if nav_eta is not None else None
        except (TypeError, ValueError):
            nav_eta = None
        snap.readings.append(HomeReading(
            name=vc.name,
            vin=vc.vin,
            lat=float(lat) if lat is not None else None,
            lon=float(lon) if lon is not None else None,
            asleep=asleep,
            nav_dest=str(nav_dest) if nav_dest else None,
            nav_eta_min=nav_eta,
        ))
    return snap


def find_vehicle(snap: FleetSnapshot, mention: str) -> HomeReading | None:
    """Best-effort vehicle resolution from a free-text mention."""
    needle = mention.strip().lower()
    if not needle:
        return None
    for r in snap.readings:
        if r.name and (needle in r.name.lower() or r.name.lower() in needle):
            return r
    return None
