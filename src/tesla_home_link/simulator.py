"""Fake evening drive home over virtual time. No credentials, no network.

Drives two pretend cars through the REAL home rules engine and the REAL
service layer (dry-run mode), proving the core behaviors:
  * approach detected from navigation, welcome scene fired once
  * HVAC pre-arrival fires early when the house is cold (gap-scaled ETA)
  * porch lights fire only after civil dusk, auto-off after arrival
  * a pass-by (nav set elsewhere / no sustained closing) fires nothing
All geography is invented (DEMO DATA — not anyone's real address).
"""
from __future__ import annotations

from dataclasses import dataclass

from .config import Settings, VehicleConfig
from .home import HomeState, civil_dusk

# Pretend geography (DEMO DATA — not anyone's real address).
HOME = (34.5794, -118.1185)

BASE_MIN_PER_READ = 20  # minutes between simulated ticks


@dataclass
class FakeVehicle:
    vin: str
    name: str
    lat: float | None = None
    lon: float | None = None
    nav_dest: str | None = None
    nav_eta_min: float | None = None
    asleep: bool = False


def _point_at(dist_m: float) -> tuple[float, float]:
    """A fake point `dist_m` north of home."""
    return HOME[0] + dist_m / 111_320.0, HOME[1]


class FakeTeslaClient:
    """Stand-in for TeslaClient with the fields this app reads."""

    def __init__(self, vehicles: list[FakeVehicle]):
        self.vehicles = {v.vin: v for v in vehicles}

    def vehicle_data(self, vin: str) -> dict:
        v = self.vehicles[vin]
        return {
            "state": "asleep" if v.asleep else "online",
            "drive_state": {"latitude": v.lat, "longitude": v.lon,
                            "shift_state": None if v.asleep else "D",
                            "active_route_destination": v.nav_dest,
                            "active_route_minutes_to_arrival": v.nav_eta_min},
        }


def demo_vehicles() -> list[FakeVehicle]:
    return [FakeVehicle(vin="VINY", name="Model Y"),
            FakeVehicle(vin="VIN3", name="Model 3")]


def demo_settings() -> Settings:
    s = Settings()
    s.vehicles = [VehicleConfig(name=v.name, vin=v.vin) for v in demo_vehicles()]
    s.home_name = "Home"
    s.home_lat, s.home_lon = HOME
    s.thermostat_backend = "webhook"
    s.thermostat_webhook = "http://localhost:9/nope"   # never called (dry-run)
    s.target_temp_f = 71.0
    s.away_setback_f = 60.0
    s.hvac_eta_min = 20
    s.lights_backend = "webhook"
    s.lights_webhook = "http://localhost:9/nope"
    s.lights_eta_min = 5
    s.lights_auto_off_min = 15
    s.tv_backend = "samsung"
    s.tv_input = "HDMI 1"
    s.tv_app = "YouTube"
    s.console_wol = True
    s.console_macs = "11:22:33:44:55:66"
    s.console_plug = True
    s.plug_webhook = "http://localhost:9/nope"
    s.alarm_webhook = "http://localhost:9/nope"
    s.dry_run = True
    s.poll_seconds = 120
    return s


# (offset vs civil dusk in minutes, vin, dist_m, nav_dest, nav_eta_min)
# Car A drives home (nav set). Car B drives PAST home to Costco (nav elsewhere).
def _scenario(dusk: float) -> list[tuple[float, str, float, str | None, float | None]]:
    a = [  # evening commute home
        (dusk - 100 * 60, "VINY", 20000, None, None),        # parked at office
        (dusk - 80 * 60,  "VINY", 16500, "Home", 28),       # leaves, nav set -> approach + welcome scene
        (dusk - 60 * 60,  "VINY", 13000, "Home", 22),       # HVAC fires (cold house: threshold 41 min)
        (dusk - 40 * 60,  "VINY", 9500, "Home", 15),
        (dusk - 20 * 60,  "VINY", 5500, "Home", 9),
        (dusk + 5 * 60,   "VINY", 2500, "Home", 4),         # lights fire (after dusk)
        (dusk + 10 * 60,  "VINY", 400, "Home", 1),
        (dusk + 14 * 60,  "VINY", 60, None, None),          # arrived
        (dusk + 30 * 60,  "VINY", 60, None, None),          # lights auto-off
    ]
    b = [  # pass-by: drives past the geofence heading to Costco
        (dusk - 80 * 60, "VIN3", 9000, "Costco", 12),
        (dusk - 60 * 60, "VIN3", 6000, "Costco", 8),
        (dusk - 40 * 60, "VIN3", 3500, "Costco", 5),
        (dusk - 20 * 60, "VIN3", 6000, "Costco", 9),
        (dusk + 5 * 60,  "VIN3", 9000, None, None),
    ]
    rows = sorted(a + b, key=lambda r: (r[0], r[1]))
    return rows


def run_simulation(settings: Settings | None = None,
                   state_path: str = ":memory:") -> dict:
    """Drive the scenario through the real service/run_cycle path.

    Returns a dict with timeline lines, action results, and an API cost
    estimate. Uses dry-run mode: no home device is touched.
    """
    from . import service as service_module
    from .service import HomeService

    settings = settings or demo_settings()
    fake = FakeTeslaClient(demo_vehicles())
    service = HomeService(settings, client_factory=lambda: fake,
                          llm_factory=lambda: None)

    import tempfile
    if state_path == ":memory:":
        state_path = tempfile.mktemp(prefix="thl-sim-state-")
    state = HomeState(state_path)

    # Indoor 64°F (cold evening), porch lights off — as a real backend would report.
    service_module.thermostat_read_indoor_f = lambda s: 64.0   # noqa: E731
    service_module.lights_currently_on = lambda s: False       # noqa: E731

    # Fixed winter-evening anchor so the demo always runs after dusk.
    dusk = civil_dusk(1_791_158_400.0, *HOME)
    assert dusk is not None

    # Group rows by tick: set every car's position, then run ONE decision
    # cycle per tick (run_cycle decides for the whole fleet at once).
    from collections import defaultdict
    ticks: dict[float, dict[str, tuple]] = defaultdict(dict)
    for tick, vin, dist_m, nav_dest, nav_eta in _scenario(dusk):
        ticks[tick][vin] = (dist_m, nav_dest, nav_eta)

    try:
        from zoneinfo import ZoneInfo
        demo_tz = ZoneInfo("America/Los_Angeles")   # pretend home: Palmdale, CA
    except Exception:
        demo_tz = None

    timeline: list[str] = []
    results: list[str] = []
    for tick in sorted(ticks):
        for vin, (dist_m, nav_dest, nav_eta) in ticks[tick].items():
            car = fake.vehicles[vin]
            car.lat, car.lon = _point_at(dist_m)
            car.nav_dest, car.nav_eta_min = nav_dest, nav_eta
        decisions, action_results = service.run_cycle(
            state=state, now=tick, dry_run=True, skip_home_devices=False)
        # run_cycle returns one decision per configured vehicle, in order
        decisions_by_vin = {vc.vin: d for vc, d in
                            zip(settings.vehicles, decisions)}
        from datetime import datetime as _dt
        clock = _dt.fromtimestamp(tick, tz=demo_tz).strftime("%-I:%M %p")
        for vin in sorted(ticks[tick]):
            car = fake.vehicles[vin]
            decision = decisions_by_vin[vin]
            mine = [r for r in action_results
                    if r.startswith(f"{car.name}:")]
            results.extend(mine)
            marker = " "
            if mine:
                marker = "★"
            else:
                nav_here = ticks[tick][vin][2] if len(ticks[tick][vin]) > 2 else None
                nav_dest_here = ticks[tick][vin][1]
                if nav_dest_here and not nav_dest_here.lower().startswith("home"):
                    marker = "⤺"     # pass-by: nav set elsewhere, suppressed
            timeline.append(f"{marker} {clock}  {car.name:8s}  {decision.reason}")
            for r in mine:
                timeline.append(f"    → {r}")
    return {"timeline": timeline, "actions": results, "state": state,
            "cost_lines": _estimate_daily_cost(settings),
            "poll_seconds": settings.poll_seconds}


def _estimate_daily_cost(settings: Settings) -> list[str]:
    # Duty cycle: 2 commute legs (~40 min each at the 120s cadence) plus
    # hourly parked checks the rest of the day. The car is READ-ONLY —
    # this app never issues a Fleet API command and never wakes the car.
    active_reads = int(2 * 40 * 60 / max(1, settings.poll_seconds))
    parked_reads = 24
    calls = active_reads + parked_reads
    day = calls / 500.0 * 1.0   # community-reported: ~$1 per 500 data calls
    lines = [
        f"Estimated daily API usage at the {settings.poll_seconds}s cadence (duty-cycled):",
        f"  ~{calls} no-wake data calls/day "
        f"({active_reads} while commuting + {parked_reads} hourly parked checks)",
        "  0 car commands + 0 wake-ups — the car is read-only; every action "
        "hits your local home devices instead.",
        f"  ≈ ${day:.2f}/day ≈ ${day * 30:.0f}/month at community-reported rates "
        "($1 per 500 data calls; re-check developer.tesla.com for live pricing).",
        "  Cut it further: raise THL_POLL_SECONDS, or run `thlink run` from cron "
        "only during your commute windows.",
    ]
    return lines


def run_demo(settings: Settings | None = None) -> list[str]:
    """Human-readable demo output for `thlink simulate`: timeline + chat Q&A,
    using the real service with the LLM off."""
    result = run_simulation(settings)
    out = ["", "Pretend evening drive home — the REAL rules engine (dry run):",
           "  Home · comfort 71°F · away 60°F · HVAC at 20 min out "
           "(gap-scaled) · lights at 5 min out after dusk", ""]
    out.extend(result["timeline"])
    out += ["",
            "Legend: ★ home-device action fired (dry run) · ⤺ pass-by suppressed",
            "", *result["cost_lines"], "", "Chat demo (rules engine, no AI needed):"]
    svc_settings = demo_settings()
    from .service import HomeService
    fake = FakeTeslaClient(demo_vehicles())
    service = HomeService(svc_settings, client_factory=lambda: fake,
                          llm_factory=lambda: None, preview_only=True)
    for q in ["why did the heat start?",
              "start the heat 30 min out",
              "what do you do?"]:
        out.append(f"> {q}")
        for line in service.answer(q, state=result["state"]).splitlines():
            out.append(f"  {line}")
    out.append("")
    return out
