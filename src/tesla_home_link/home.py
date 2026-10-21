"""Pure home-continuity decision logic. No network here — `now` and all
inputs are injected, so every branch is unit-testable.

The public entry point is ``decide()``, which takes a vehicle reading and
the persistent state and returns a HomeDecision with a list of home-device
actions (executed by the service layer against the configured backends).

Trip phases per car: "unknown" -> "away" -> "approaching" -> "home" -> "away".

  * Detection: primary signal = active navigation destination ~= home.
    Fallback = closing-distance detector: >=3 consecutive reads with
    strictly decreasing distance, inside the outer approach geofence.
    Pass-by suppression: a drive that crosses the geofence without
    sustained closing (distance bottoms out and rises again), or whose
    navigation points anywhere else, never fires.
  * HVAC: fires once per trip when ETA <= HVAC_ETA_MIN + min(temp_gap_F*3, 30)
    (bigger gaps start earlier, capped at +30 min). The fired setpoint is
    recorded so an aborted trip can restore the previous setpoint.
  * Lights: fires once per trip when ETA <= LIGHTS_ETA_MIN *and* it is after
    civil dusk (sun 6 deg below horizon, computed from date + home coords —
    no point lighting the porch at noon), unless the lights were already on.
    Auto-off N minutes after arrival unless they were already on before.
  * Welcome scene: on the approach trigger — TV on + input + app, console
    Wake-on-LAN, smart-plug power restore; each individually toggleable.
  * Leave-home: once the car is >1 km out and receding, fire away actions
    once per departure (thermostat setback, lights off, alarm webhook).
  * Abort: the approach diverts (nav points away from home, or the car is
    parked/receding for >10 min) -> restore the HVAC setpoint and log it.
  * Exactly-once per trip: flags reset on arrival and departure.
"""
from __future__ import annotations

import json
import math
import os
import time
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

EARTH_RADIUS_M = 6_371_000.0
ABORT_REcede_SECONDS = 10 * 60
AWAY_TRIGGER_M = 1000.0


# -- geometry ------------------------------------------------------------

def haversine_m(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """Great-circle distance in metres between two lat/lon points."""
    phi1, phi2 = math.radians(lat1), math.radians(lat2)
    dphi = math.radians(lat2 - lat1)
    dlambda = math.radians(lon2 - lon1)
    a = (math.sin(dphi / 2) ** 2
         + math.cos(phi1) * math.cos(phi2) * math.sin(dlambda / 2) ** 2)
    return 2 * EARTH_RADIUS_M * math.asin(math.sqrt(a))


# -- sun position (no dependencies) ---------------------------------------

def _civil_dusk_utc(day_epoch_midnight: float, lat: float, lon: float) -> float | None:
    """Epoch seconds (UTC) of civil dusk — the moment the sun sinks 6 deg
    below the horizon — for the UTC calendar day starting at
    `day_epoch_midnight`. Returns None if the sun never reaches 6 deg below
    the horizon that day (polar day). Standard NOAA sunrise/sunset algorithm.
    """
    zenith = math.radians(96.0)
    n = math.ceil((day_epoch_midnight - _epoch_of(1970, 1, 1)) / 86400.0)
    lng_hour = lon / 15.0
    t = n + (18.0 - lng_hour) / 24.0          # approx time, sunset branch
    m = 0.9856 * t - 3.289
    m_rad = math.radians(m)
    l = (m + 1.916 * math.sin(m_rad) + 0.020 * math.sin(2 * m_rad)
         + 282.634) % 360.0
    l_rad = math.radians(l)
    ra = math.degrees(math.atan(0.91764 * math.tan(l_rad))) % 360.0
    l_quad = (math.floor(l / 90.0) * 90.0)
    ra_quad = (math.floor(ra / 90.0) * 90.0)
    ra = (ra + (l_quad - ra_quad)) / 15.0
    sin_dec = 0.39782 * math.sin(l_rad)
    cos_dec = math.cos(math.asin(sin_dec))
    lat_rad = math.radians(lat)
    cos_h = ((math.cos(zenith) - sin_dec * math.sin(lat_rad))
             / (cos_dec * math.cos(lat_rad)))
    if cos_h > 1.0:
        return None       # sun never gets 6 deg below the horizon today
    if cos_h < -1.0:
        return day_epoch_midnight  # polar night — dusk already happened
    h = math.degrees(math.acos(cos_h)) / 15.0   # sunset branch: +H
    t_local = h + ra - (0.06571 * t) - 6.622
    ut = (t_local - lng_hour) % 24.0
    return day_epoch_midnight + ut * 3600.0


def _epoch_of(year: int, month: int, day: int) -> float:
    return datetime(year, month, day, tzinfo=timezone.utc).timestamp()


def is_after_civil_dusk(now: float, lat: float, lon: float) -> bool:
    """True when it is currently dark at home: the most recent civil-dusk
    moment (sun 6 deg below the horizon) passed less than 12 hours ago.

    The 12h window matters because dusk events fall on UTC calendar days:
    without it, 1 PM would count as "after yesterday evening's dusk".
    """
    dt = datetime.fromtimestamp(now, tz=timezone.utc)
    base = _epoch_of(dt.year, dt.month, dt.day)
    candidates = []
    for delta in (-86400.0, 0.0, 86400.0):
        dusk = _civil_dusk_utc(base + delta, lat, lon)
        if dusk is not None:
            candidates.append(dusk)
    recent = [c for c in candidates if c <= now]
    if not recent:
        return False      # polar day — never dark
    return (now - max(recent)) < 12 * 3600


def civil_dusk(now: float, lat: float, lon: float) -> float | None:
    """Civil-dusk epoch for the UTC calendar day containing `now`."""
    dt = datetime.fromtimestamp(now, tz=timezone.utc)
    return _civil_dusk_utc(_epoch_of(dt.year, dt.month, dt.day), lat, lon)


# -- persistent state ------------------------------------------------------

@dataclass
class VehicleTripState:
    phase: str = "unknown"               # unknown|away|approaching|home
    trip_id: int = 0
    reads: list = field(default_factory=list)   # [[ts, dist_m], ...] newest last (<=6)
    hvac_fired: bool = False
    hvac_fired_setpoint: float | None = None
    hvac_prev_setpoint: float | None = None
    lights_fired: bool = False
    lights_were_on: bool = False
    lights_off_at: float | None = None
    welcome_fired: bool = False
    away_fired: bool = False
    arrived_ts: float | None = None
    last_nav_dest: str | None = None
    approach_since: float | None = None
    log: list = field(default_factory=list)     # [[ts, text], ...] append-only


class HomeState:
    """Small JSON state file: trip phases, exactly-once flags, decision log."""

    def __init__(self, path: str):
        self.path = Path(os.path.expanduser(path))
        self.vehicles: dict[str, VehicleTripState] = {}
        self.meta: dict = {}
        self._load()

    def _load(self) -> None:
        try:
            raw = json.loads(self.path.read_text())
        except (OSError, json.JSONDecodeError):
            raw = {}
        if not isinstance(raw, dict):
            raw = {}
        self.meta = dict(raw.get("meta") or {})
        for vin, entry in (raw.get("vehicles") or {}).items():
            if not isinstance(entry, dict):
                continue
            self.vehicles[str(vin)] = VehicleTripState(
                phase=str(entry.get("phase", "unknown")),
                trip_id=int(entry.get("trip_id", 0)),
                reads=list(entry.get("reads") or [])[-6:],
                hvac_fired=bool(entry.get("hvac_fired")),
                hvac_fired_setpoint=entry.get("hvac_fired_setpoint"),
                hvac_prev_setpoint=entry.get("hvac_prev_setpoint"),
                lights_fired=bool(entry.get("lights_fired")),
                lights_were_on=bool(entry.get("lights_were_on")),
                lights_off_at=entry.get("lights_off_at"),
                welcome_fired=bool(entry.get("welcome_fired")),
                away_fired=bool(entry.get("away_fired")),
                arrived_ts=entry.get("arrived_ts"),
                last_nav_dest=entry.get("last_nav_dest"),
                approach_since=entry.get("approach_since"),
                log=list(entry.get("log") or [])[-500:],
            )

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        def dump(vs: VehicleTripState) -> dict:
            return {"phase": vs.phase, "trip_id": vs.trip_id,
                    "reads": list(vs.reads)[-6:],
                    "hvac_fired": vs.hvac_fired,
                    "hvac_fired_setpoint": vs.hvac_fired_setpoint,
                    "hvac_prev_setpoint": vs.hvac_prev_setpoint,
                    "lights_fired": vs.lights_fired,
                    "lights_were_on": vs.lights_were_on,
                    "lights_off_at": vs.lights_off_at,
                    "welcome_fired": vs.welcome_fired,
                    "away_fired": vs.away_fired,
                    "arrived_ts": vs.arrived_ts,
                    "last_nav_dest": vs.last_nav_dest,
                    "approach_since": vs.approach_since,
                    "log": list(vs.log)[-500:]}
        self.path.write_text(json.dumps(
            {"meta": self.meta,
             "vehicles": {vin: dump(vs) for vin, vs in self.vehicles.items()}},
            indent=2))
        try:
            os.chmod(self.path, 0o600)
        except OSError:
            pass

    def for_vin(self, vin: str) -> VehicleTripState:
        return self.vehicles.setdefault(vin, VehicleTripState())


# -- readings / decisions ---------------------------------------------------

@dataclass
class HomeReading:
    """One vehicle read, enriched with home-device context (injected)."""
    name: str
    vin: str
    lat: float | None
    lon: float | None
    asleep: bool = False
    nav_dest: str | None = None          # active navigation destination text
    nav_eta_min: float | None = None     # minutes to arrival, from the car
    indoor_temp_f: float | None = None   # current indoor temp from the thermostat
    lights_on: bool | None = None        # porch/entry lights state, if known


@dataclass
class HomeAction:
    kind: str            # hvac_set | hvac_restore | lights_on | lights_off |
                         # tv_on | console_wake | plug_on | away_setback | alarm_arm
    params: dict = field(default_factory=dict)
    label: str = ""      # plain-English summary of what it does


@dataclass
class HomeDecision:
    actions: list[HomeAction] = field(default_factory=list)
    reason: str = ""
    events: list[str] = field(default_factory=list)   # what changed this tick


def nav_is_home(nav_dest: str | None, home_name: str) -> bool:
    """Best-effort: does the car's navigation destination mean 'home'?"""
    if not nav_dest:
        return False
    dest = nav_dest.strip().lower()
    needle = home_name.strip().lower()
    if needle and (needle in dest or dest in needle):
        return True
    return dest == "home"


def _closing_reads(reads: list, n: int = 3) -> bool:
    """True when the last n distances are strictly decreasing."""
    if len(reads) < n:
        return False
    tail = [r[1] for r in reads[-n:]]
    return all(a > b for a, b in zip(tail, tail[1:]))


def _receding_reads(reads: list, n: int = 3) -> bool:
    """True when the last n distances are strictly increasing."""
    if len(reads) < n:
        return False
    tail = [r[1] for r in reads[-n:]]
    return all(a < b for a, b in zip(tail, tail[1:]))


def _estimate_eta_min(reads: list) -> float | None:
    """Implied ETA from the closing rate across the stored reads."""
    if len(reads) < 2:
        return None
    t0, d0 = reads[0]
    t1, d1 = reads[-1]
    dt = t1 - t0
    if dt <= 0:
        return None
    rate_mps = (d0 - d1) / dt          # positive when closing
    if rate_mps <= 0:
        return None
    return d1 / rate_mps / 60.0


def _clock(now: float) -> str:
    return datetime.fromtimestamp(now).strftime("%-I:%M%p").lower()


# -- the decision ------------------------------------------------------------

def decide(reading: HomeReading, state: HomeState, settings,
           now: float | None = None) -> HomeDecision:
    """Decide what (if anything) to do about one car's home approach."""
    now = time.time() if now is None else now
    vs = state.for_vin(reading.vin)
    dec = HomeDecision()

    def log(text: str) -> None:
        vs.log.append([now, text])
        dec.events.append(text)

    if reading.asleep:
        dec.reason = (f"{reading.name} is asleep — nothing to track right now; "
                      "will check again on the next cycle.")
        return dec
    if reading.lat is None or reading.lon is None:
        dec.reason = f"{reading.name} has no location in this read — skipping."
        return dec

    dist = haversine_m(settings.home_lat, settings.home_lon,
                       reading.lat, reading.lon)
    vs.reads = (vs.reads + [[now, dist]])[-6:]
    vs.last_nav_dest = reading.nav_dest

    # --- lights auto-off after arrival ------------------------------------
    # (checked before the arrival branch so the scheduled off isn't
    # swallowed by the "already home" early return)
    if (vs.phase == "home" and vs.lights_fired and vs.lights_off_at
            and now >= vs.lights_off_at):
        vs.lights_fired = False
        vs.lights_off_at = None
        dec.actions.append(HomeAction(kind="lights_off",
                                      label="turn the entry/porch lights back off"))
        log(f"Lights auto-off at {_clock(now)} ({settings.lights_auto_off_min} "
            "minutes after arrival).")
        dec.reason = (f"{reading.name} is home — turning the porch lights back "
                      "off after the arrival window.")
        return dec

    # --- arrival: inside the home radius -----------------------------------
    if dist <= settings.arrival_radius_m:
        if vs.phase != "home":
            vs.phase = "home"
            vs.arrived_ts = now
            vs.hvac_fired = False
            vs.welcome_fired = False
            vs.away_fired = False
            # lights auto-off N minutes after arrival, unless they were
            # already on before we triggered them
            if vs.lights_fired and not vs.lights_were_on:
                vs.lights_off_at = now + settings.lights_auto_off_min * 60
            log(f"Arrived home at {_clock(now)} — approach run complete.")
        dec.reason = f"{reading.name} is home — nothing to trigger."
        return dec

    # --- leave-home: >1 km out and receding, once per departure ------------
    # Only a car that was HOME can depart: a drive that merely passes the
    # neighbourhood (never arrived) must not flip the house to away mode.
    receding = _receding_reads(vs.reads, 3)
    if vs.phase == "home" and dist > AWAY_TRIGGER_M and receding and not vs.away_fired:
        vs.away_fired = True
        vs.phase = "away"
        vs.trip_id += 1
        # reset the approach flags for the next trip home
        vs.hvac_fired = False
        vs.lights_fired = False
        vs.welcome_fired = False
        vs.lights_off_at = None
        vs.approach_since = None
        log(f"Left home at {_clock(now)} — firing away actions.")
        dec.actions.append(HomeAction(
            kind="away_setback",
            params={"setpoint_f": settings.away_setback_f},
            label=f"set the thermostat back to {settings.away_setback_f:g}°F (away)"))
        dec.actions.append(HomeAction(kind="lights_off",
                                      label="turn the entry/porch lights off"))
        if settings.alarm_webhook:
            dec.actions.append(HomeAction(kind="alarm_arm",
                                          label="arm the alarm (webhook)"))
        dec.reason = (f"{reading.name} left home — setting the house to away "
                      "mode.")
        return dec

    # --- approach detection -------------------------------------------------
    nav_home = nav_is_home(reading.nav_dest, settings.home_name)
    closing = (_closing_reads(vs.reads, 3)
               and dist <= settings.approach_geofence_m
               and not (reading.nav_dest and not nav_home))
    approaching = nav_home or closing

    if approaching and vs.phase != "approaching":
        vs.phase = "approaching"
        vs.approach_since = now
        how = (f"navigation destination is {reading.nav_dest!r}"
               if nav_home else "closing-distance detector (3+ shrinking reads)")
        log(f"Approach detected at {_clock(now)} — {how}.")
        # welcome scene: each device individually toggleable in the config
        if settings.tv_backend != "none" and not vs.welcome_fired:
            parts = ["turn the TV on"]
            if settings.tv_input:
                parts.append(f"switch to {settings.tv_input}")
            if settings.tv_app:
                parts.append(f"open {settings.tv_app}")
            dec.actions.append(HomeAction(kind="tv_on", label=", ".join(parts)))
        if settings.console_wol and settings.console_macs:
            dec.actions.append(HomeAction(
                kind="console_wake",
                params={"macs": settings.console_macs},
                label="wake the game console(s) from standby (Wake-on-LAN)"))
        if settings.console_plug and settings.plug_webhook:
            dec.actions.append(HomeAction(
                kind="plug_on",
                label="restore power to the console smart plug"))
        if dec.actions:
            vs.welcome_fired = True
        dec.reason = (f"{reading.name} is on its way home — welcome scene "
                      "started.")
        return dec

    # --- abort: the trip diverted -------------------------------------------
    diverted = False
    divert_why = ""
    if vs.phase == "approaching":
        if reading.nav_dest and not nav_home:
            diverted = True
            divert_why = (f"navigation changed to {reading.nav_dest!r} "
                          "(no longer heading home)")
        elif (vs.approach_since is not None
              and now - vs.approach_since > ABORT_REcede_SECONDS
              and _receding_reads(vs.reads)):
            diverted = True
            divert_why = "the car has been moving away from home for 10+ minutes"
    if diverted:
        vs.phase = "away"
        vs.trip_id += 1
        vs.approach_since = None
        vs.hvac_fired = False
        vs.lights_fired = False
        vs.welcome_fired = False
        log(f"Trip aborted at {_clock(now)} — {divert_why}.")
        if vs.hvac_fired_setpoint is not None and vs.hvac_prev_setpoint is not None:
            dec.actions.append(HomeAction(
                kind="hvac_restore",
                params={"setpoint_f": vs.hvac_prev_setpoint},
                label=f"restore the thermostat to {vs.hvac_prev_setpoint:g}°F "
                      "(trip diverted)"))
            log(f"Restored the thermostat to {vs.hvac_prev_setpoint:g}°F — "
                "the trip to home was aborted.")
        else:
            log("Nothing to restore — the HVAC pre-heat hadn't fired yet.")
        vs.hvac_fired_setpoint = None
        vs.hvac_prev_setpoint = None
        dec.reason = (f"{reading.name} is no longer heading home "
                      f"({divert_why}) — any pre-arrival changes rolled back.")
        return dec

    # --- while approaching: HVAC and lights ---------------------------------
    if vs.phase == "approaching":
        eta = reading.nav_eta_min if nav_home else _estimate_eta_min(vs.reads)
        eta_text = f"{eta:.0f} min out" if eta is not None else "ETA unknown"

        # HVAC pre-arrival: threshold scales with the temperature gap so a
        # cold house gets a head start (capped at +30 minutes).
        if not vs.hvac_fired and settings.thermostat_backend != "none":
            gap = 0.0
            if reading.indoor_temp_f is not None:
                gap = abs(settings.target_temp_f - reading.indoor_temp_f)
            threshold = settings.hvac_eta_min + min(gap * 3.0, 30.0)
            if eta is not None and eta <= threshold:
                vs.hvac_fired = True
                vs.hvac_fired_setpoint = settings.target_temp_f
                vs.hvac_prev_setpoint = reading.indoor_temp_f
                indoor_text = (f" (it's {reading.indoor_temp_f:g}°F inside)"
                               if reading.indoor_temp_f is not None else "")
                dec.actions.append(HomeAction(
                    kind="hvac_set",
                    params={"setpoint_f": settings.target_temp_f},
                    label=f"pre-heat/cool the house to {settings.target_temp_f:g}°F "
                          f"(ETA {eta:.0f} min{indoor_text})"))
                log(f"HVAC pre-arrival fired at {_clock(now)}: ETA home "
                    f"crossed {threshold:.0f} min (ETA {eta:.0f} min), indoor "
                    f"{reading.indoor_temp_f}°F → target "
                    f"{settings.target_temp_f:g}°F.")
                dec.reason = (f"{reading.name} is {eta_text} — starting the "
                              "house climate early so it's comfortable when "
                              "you walk in.")
                return dec

        # Lights: only after civil dusk, unless already on.
        if not vs.lights_fired and settings.lights_backend != "none":
            already_on = bool(reading.lights_on)
            if eta is not None and eta <= settings.lights_eta_min:
                if already_on:
                    vs.lights_fired = True
                    vs.lights_were_on = True
                    log(f"Porch lights already on at {_clock(now)} — no trigger "
                        "needed.")
                elif is_after_civil_dusk(now, settings.home_lat, settings.home_lon):
                    vs.lights_fired = True
                    vs.lights_were_on = False
                    dec.actions.append(HomeAction(
                        kind="lights_on",
                        label=f"turn the entry/porch lights on (ETA {eta:.0f} "
                              "min, after dusk)"))
                    log(f"Lights on at {_clock(now)} — ETA home crossed "
                        f"{settings.lights_eta_min} min after civil dusk.")
                    dec.reason = (f"{reading.name} is {eta_text} and it's dark — "
                                  "turning the porch lights on.")
                    return dec

        dec.reason = (f"{reading.name} is heading home ({eta_text}) — climate "
                      "and lights already handled for this trip, watching.")
        return dec

    # --- parked/away, nothing to do ------------------------------------------
    dec.reason = (f"{reading.name} is {int(dist)} m from home and not heading "
                  "here — nothing to trigger.")
    return dec
