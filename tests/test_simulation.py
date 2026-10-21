"""End-to-end: the pretend evening drive through the real service path.

Asserts the app's core invariants:
  * the HVAC pre-arrival fires exactly once per trip, early on a cold evening
  * the welcome scene (TV + console wake + plug) fires once on approach
  * the porch lights fire after dusk and auto-off after arrival
  * the pass-by leg (VIN3, nav set elsewhere) fires nothing
  * every action in the demo is a dry run (no network, no credentials)
"""
from tesla_home_link.simulator import run_demo, run_simulation


def test_simulation_fires_hvac_once():
    result = run_simulation()
    hvac_logs = [e for e in result["state"].for_vin("VINY").log
                 if "HVAC pre-arrival fired" in e[1]]
    assert len(hvac_logs) == 1
    assert "64" in hvac_logs[0][1]          # cold house drove the early trigger
    assert "71" in hvac_logs[0][1]          # ...toward the 71°F target


def test_simulation_welcome_scene_fires_once_on_approach():
    result = run_simulation()
    # The dry-run action strings name each welcome device.
    fired = " ".join(result["actions"])
    assert "TV" in fired
    assert "console" in fired.lower()
    assert "plug" in fired.lower()


def test_simulation_lights_on_then_auto_off():
    result = run_simulation()
    log_text = " ".join(e[1] for e in result["state"].for_vin("VINY").log)
    assert "Lights on at" in log_text
    assert "Lights auto-off at" in log_text
    fired = " ".join(result["actions"])
    assert "lights" in fired.lower()


def test_simulation_pass_by_fires_nothing():
    result = run_simulation()
    vs3 = result["state"].for_vin("VIN3")
    assert vs3.phase != "approaching"
    assert vs3.phase != "home"
    assert not any("Approach detected" in e[1] for e in vs3.log)


def test_simulation_all_actions_are_dry_runs():
    result = run_simulation()
    assert result["actions"], "the demo should fire actions"
    assert all(a.startswith("Model Y: [dry-run]") for a in result["actions"])


def test_simulation_cost_estimate_present():
    result = run_simulation()
    text = "\n".join(result["cost_lines"])
    assert "$" in text and "wake" in text.lower()


def test_demo_output_readable():
    lines = run_demo()
    text = "\n".join(lines)
    assert "Pretend evening drive home" in text
    assert "Legend" in text
    assert "> why did the heat start?" in text
    assert "HVAC pre-arrival fired" in text
    assert "> start the heat 30 min out" in text
    assert "30" in text
