"""Pure-logic tests for the home approach detector and triggers.

Every branch runs against injected `now` — no network, no clock.
"""
from tesla_home_link.config import Settings
from tesla_home_link.home import (HomeReading, HomeState, civil_dusk, decide,
                                  haversine_m, is_after_civil_dusk, nav_is_home)

HOME_LAT, HOME_LON = 34.5794, -118.1185


def _settings(**over):
    s = Settings()
    s.home_lat, s.home_lon = HOME_LAT, HOME_LON
    s.thermostat_backend = "webhook"
    s.thermostat_webhook = "http://x/heat"
    s.lights_backend = "webhook"
    s.lights_webhook = "http://x/lights"
    s.target_temp_f = 71.0
    s.away_setback_f = 60.0
    s.hvac_eta_min = 20
    s.lights_eta_min = 5
    s.lights_auto_off_min = 15
    for k, v in over.items():
        setattr(s, k, v)
    return s


def _lat(dist_m: float) -> float:
    return HOME_LAT + dist_m / 111_320.0


def _reading(dist_m: float, now: float = 1000.0, vin: str = "VINY",
             nav_dest=None, nav_eta=None, indoor=None, lights_on=None) -> HomeReading:
    return HomeReading(name="Model Y", vin=vin, lat=_lat(dist_m), lon=HOME_LON,
                       nav_dest=nav_dest, nav_eta_min=nav_eta,
                       indoor_temp_f=indoor, lights_on=lights_on)


def _state(tmp_path) -> HomeState:
    return HomeState(str(tmp_path / "state.json"))


# -- geometry / nav ----------------------------------------------------------

def test_haversine_zero():
    assert haversine_m(HOME_LAT, HOME_LON, HOME_LAT, HOME_LON) == 0.0


def test_nav_is_home_variants():
    assert nav_is_home("Home", "Home")
    assert nav_is_home("home", "Home")
    assert nav_is_home("  HOME  ", "Home")
    assert not nav_is_home("Costco", "Home")
    assert not nav_is_home(None, "Home")
    assert not nav_is_home("", "Home")
    assert nav_is_home("Casa", "Casa")


# -- approach detection --------------------------------------------------------

def test_nav_home_triggers_approach_and_welcome_scene(tmp_path):
    s = _settings(tv_backend="samsung", tv_input="HDMI 1",
                  console_wol=True, console_macs="11:22:33:44:55:66",
                  console_plug=True, plug_webhook="http://x/plug")
    st = _state(tmp_path)
    dec = decide(_reading(15000, nav_dest="Home", nav_eta=25), st, s, now=1000.0)
    kinds = [a.kind for a in dec.actions]
    assert "tv_on" in kinds
    assert "console_wake" in kinds
    assert "plug_on" in kinds
    assert st.for_vin("VINY").phase == "approaching"
    assert any("Approach detected" in e for e in dec.events)


def test_fallback_needs_exactly_three_closing_reads(tmp_path):
    s = _settings()
    st = _state(tmp_path)
    t = 1000.0
    d1 = decide(_reading(9000, now=t), st, s, now=t)
    assert st.for_vin("VINY").phase != "approaching"
    d2 = decide(_reading(8000, now=t + 60), st, s, now=t + 60)
    assert st.for_vin("VINY").phase != "approaching"
    d3 = decide(_reading(7000, now=t + 120), st, s, now=t + 120)
    assert st.for_vin("VINY").phase == "approaching"
    assert any("closing-distance" in e for e in d3.events)


def test_fallback_broken_chain_does_not_fire(tmp_path):
    s = _settings()
    st = _state(tmp_path)
    t = 1000.0
    # Distances dip but never sustain 3 consecutive decreases.
    for i, dist in enumerate([9000, 7000, 7200, 8000, 9500]):
        decide(_reading(dist, now=t + i * 60), st, s, now=t + i * 60)
    assert st.for_vin("VINY").phase != "approaching"


def test_pass_by_with_nav_elsewhere_suppressed(tmp_path):
    s = _settings()
    st = _state(tmp_path)
    t = 1000.0
    dec = None
    for i, dist in enumerate([9000, 6000, 3500]):   # closing, but nav says Costco
        dec = decide(_reading(dist, now=t + i * 60, nav_dest="Costco",
                              nav_eta=8 - i * 2), st, s, now=t + i * 60)
    assert st.for_vin("VINY").phase != "approaching"
    assert dec.actions == []


def test_pass_by_without_nav_suppressed(tmp_path):
    s = _settings()
    st = _state(tmp_path)
    t = 1000.0
    for i, dist in enumerate([9000, 7000, 7500, 9000]):
        decide(_reading(dist, now=t + i * 60), st, s, now=t + i * 60)
    assert st.for_vin("VINY").phase != "approaching"


# -- HVAC ----------------------------------------------------------------------

def test_hvac_fires_at_gap_scaled_threshold(tmp_path):
    s = _settings()
    st = _state(tmp_path)
    # indoor 64 -> threshold = 20 + min(7*3, 30) = 41; eta 30 <= 41 fires
    decide(_reading(15000, nav_dest="Home", nav_eta=45, indoor=64.0), st, s, now=1000.0)
    dec = decide(_reading(12000, now=1060.0, nav_dest="Home", nav_eta=30,
                          indoor=64.0), st, s, now=1060.0)
    kinds = [a.kind for a in dec.actions]
    assert "hvac_set" in kinds
    assert dec.actions[0].params["setpoint_f"] == 71.0
    assert any("HVAC pre-arrival fired" in e for e in dec.events)


def test_hvac_waits_when_house_already_warm(tmp_path):
    s = _settings()
    st = _state(tmp_path)
    # indoor 70 -> threshold = 20 + 3 = 23; eta 30 > 23 -> no fire
    decide(_reading(15000, nav_dest="Home", nav_eta=45, indoor=70.0), st, s, now=1000.0)
    dec = decide(_reading(12000, now=1060.0, nav_dest="Home", nav_eta=30,
                          indoor=70.0), st, s, now=1060.0)
    assert "hvac_set" not in [a.kind for a in dec.actions]


def test_hvac_exactly_once_per_trip(tmp_path):
    s = _settings()
    st = _state(tmp_path)
    decide(_reading(15000, nav_dest="Home", nav_eta=25, indoor=64.0), st, s, now=1000.0)
    d1 = decide(_reading(12000, now=1060.0, nav_dest="Home", nav_eta=18,
                         indoor=64.0), st, s, now=1060.0)
    assert "hvac_set" in [a.kind for a in d1.actions]
    d2 = decide(_reading(9000, now=1120.0, nav_dest="Home", nav_eta=12,
                         indoor=64.0), st, s, now=1120.0)
    assert "hvac_set" not in [a.kind for a in d2.actions]


# -- abort ---------------------------------------------------------------------

def test_abort_restores_setpoint(tmp_path):
    s = _settings()
    st = _state(tmp_path)
    decide(_reading(15000, nav_dest="Home", nav_eta=25, indoor=64.0), st, s, now=1000.0)
    decide(_reading(12000, now=1060.0, nav_dest="Home", nav_eta=18,
                    indoor=64.0), st, s, now=1060.0)   # HVAC fires (prev 64)
    dec = decide(_reading(11000, now=1120.0, nav_dest="Costco", nav_eta=20,
                          indoor=64.0), st, s, now=1120.0)
    kinds = [a.kind for a in dec.actions]
    assert "hvac_restore" in kinds
    assert dec.actions[kinds.index("hvac_restore")].params["setpoint_f"] == 64.0
    assert any("Restored the thermostat" in e for e in dec.events)


def test_abort_before_hvac_has_nothing_to_restore(tmp_path):
    s = _settings()
    st = _state(tmp_path)
    decide(_reading(15000, nav_dest="Home", nav_eta=50, indoor=64.0), st, s, now=1000.0)
    dec = decide(_reading(14000, now=1060.0, nav_dest="Costco", nav_eta=20,
                          indoor=64.0), st, s, now=1060.0)
    assert "hvac_restore" not in [a.kind for a in dec.actions]
    assert any("hadn't fired yet" in e for e in dec.events)


# -- lights ---------------------------------------------------------------------

def _dusk_times():
    dusk = civil_dusk(1_791_158_400.0, HOME_LAT, HOME_LON)  # Oct 4 evening PDT
    assert dusk is not None
    return dusk - 6 * 3600, dusk + 3600


def _approach(s, st, now):
    decide(_reading(15000, now=now, nav_dest="Home", nav_eta=25), st, s, now=now)


def test_lights_fire_after_dusk_not_before(tmp_path):
    day, night = _dusk_times()
    assert not is_after_civil_dusk(day, HOME_LAT, HOME_LON)
    assert is_after_civil_dusk(night, HOME_LAT, HOME_LON)

    s = _settings(thermostat_backend="none")
    st = _state(tmp_path)
    _approach(s, st, day)
    dec = decide(_reading(2000, now=day + 60, nav_dest="Home", nav_eta=4,
                          lights_on=False), st, s, now=day + 60)
    assert "lights_on" not in [a.kind for a in dec.actions]

    st2 = _state(tmp_path / "n2")
    s2 = _settings(thermostat_backend="none")
    _approach(s2, st2, night)
    dec2 = decide(_reading(2000, now=night + 60, nav_dest="Home", nav_eta=4,
                           lights_on=False), st2, s2, now=night + 60)
    assert "lights_on" in [a.kind for a in dec2.actions]


def test_lights_skipped_when_already_on(tmp_path):
    _, night = _dusk_times()
    s = _settings(thermostat_backend="none")
    st = _state(tmp_path)
    _approach(s, st, night)
    dec = decide(_reading(2000, now=night + 60, nav_dest="Home", nav_eta=4,
                          lights_on=True), st, s, now=night + 60)
    assert "lights_on" not in [a.kind for a in dec.actions]
    assert st.for_vin("VINY").lights_were_on is True


def test_lights_auto_off_after_arrival(tmp_path):
    _, night = _dusk_times()
    s = _settings(thermostat_backend="none")
    st = _state(tmp_path)
    _approach(s, st, night)
    decide(_reading(2000, now=night + 60, nav_dest="Home", nav_eta=4,
                    lights_on=False), st, s, now=night + 60)
    assert st.for_vin("VINY").lights_fired
    arrived = night + 300
    decide(_reading(60, now=arrived), st, s, now=arrived)          # arrival
    assert st.for_vin("VINY").lights_off_at == arrived + 15 * 60
    early = decide(_reading(60, now=arrived + 5 * 60), st, s, now=arrived + 5 * 60)
    assert "lights_off" not in [a.kind for a in early.actions]
    late = decide(_reading(60, now=arrived + 16 * 60), st, s, now=arrived + 16 * 60)
    assert "lights_off" in [a.kind for a in late.actions]
    later = decide(_reading(60, now=arrived + 20 * 60), st, s, now=arrived + 20 * 60)
    assert "lights_off" not in [a.kind for a in later.actions]   # exactly once


def test_lights_no_auto_off_when_were_already_on(tmp_path):
    _, night = _dusk_times()
    s = _settings(thermostat_backend="none")
    st = _state(tmp_path)
    _approach(s, st, night)
    decide(_reading(2000, now=night + 60, nav_dest="Home", nav_eta=4,
                    lights_on=True), st, s, now=night + 60)
    arrived = night + 300
    decide(_reading(60, now=arrived), st, s, now=arrived)
    late = decide(_reading(60, now=arrived + 30 * 60), st, s, now=arrived + 30 * 60)
    assert "lights_off" not in [a.kind for a in late.actions]


# -- leave-home -------------------------------------------------------------------

def test_leave_home_fires_away_actions_once(tmp_path):
    s = _settings(alarm_webhook="http://x/alarm")
    st = _state(tmp_path)
    t = 1000.0
    decide(_reading(60, now=t), st, s, now=t)                    # home
    d1 = decide(_reading(1500, now=t + 60), st, s, now=t + 60)
    assert "away_setback" not in [a.kind for a in d1.actions]   # needs 3 receding reads
    d2 = decide(_reading(2500, now=t + 120), st, s, now=t + 120)
    kinds = [a.kind for a in d2.actions]
    assert "away_setback" in kinds
    assert "lights_off" in kinds
    assert "alarm_arm" in kinds
    # exactly once per departure
    d3 = decide(_reading(3500, now=t + 180), st, s, now=t + 180)
    assert "away_setback" not in [a.kind for a in d3.actions]


def test_leave_home_needs_receding(tmp_path):
    s = _settings()
    st = _state(tmp_path)
    # A car that was never home and isn't receding: no away actions.
    dec = decide(_reading(2000, now=1000.0), st, s, now=1000.0)
    assert "away_setback" not in [a.kind for a in dec.actions]
    assert st.for_vin("VINY").phase != "away"


# -- misc --------------------------------------------------------------------------

def test_asleep_and_no_location_skip(tmp_path):
    s = _settings()
    st = _state(tmp_path)
    r = _reading(5000); r.asleep = True
    dec = decide(r, st, s, now=1000.0)
    assert dec.actions == [] and "asleep" in dec.reason
    r2 = HomeReading(name="Model Y", vin="VINY", lat=None, lon=None)
    dec2 = decide(r2, st, s, now=1000.0)
    assert dec2.actions == []


def test_arrival_resets_trip_flags(tmp_path):
    s = _settings()
    st = _state(tmp_path)
    decide(_reading(15000, nav_dest="Home", nav_eta=25, indoor=64.0), st, s, now=1000.0)
    vs = st.for_vin("VINY")
    decide(_reading(60, now=2000.0), st, s, now=2000.0)   # arrive
    assert vs.phase == "home"
    assert vs.hvac_fired is False
    assert vs.away_fired is False
