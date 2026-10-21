"""Config parsing: THL_ env vars, defaults, and validation errors."""
import pytest

from tesla_home_link.config import ConfigError, load_settings


def test_defaults():
    s = load_settings({})
    assert s.home_name == "Home"
    assert s.hvac_eta_min == 20
    assert s.lights_eta_min == 5
    assert s.lights_auto_off_min == 15
    assert s.target_temp_f == 71.0
    assert s.away_setback_f == 60.0
    assert s.thermostat_backend == "none"
    assert s.lights_backend == "none"
    assert s.tv_backend == "none"
    assert s.server_port == 8644
    assert s.model_id == "tesla-home-link"
    assert s.ai_base_url == "http://localhost:11434/v1"
    assert s.ai_model == "llama3.1"
    assert s.ai_mode == "auto"
    assert s.poll_seconds == 120
    assert s.dry_run is False
    assert s.vehicles == []


def test_full_parse():
    s = load_settings({
        "THL_HOME_NAME": "Casa",
        "THL_HOME_ADDRESS": "1 Main St",
        "THL_HOME_LAT": "34.5",
        "THL_HOME_LON": "-118.1",
        "THL_THERMOSTAT_BACKEND": "webhook",
        "THL_THERMOSTAT_WEBHOOK": "http://x/heat",
        "THL_TARGET_TEMP_F": "72",
        "THL_HVAC_ETA_MIN": "30",
        "THL_LIGHTS_BACKEND": "hue",
        "THL_HUE_BRIDGE_IP": "192.168.1.2",
        "THL_HUE_API_KEY": "abc",
        "THL_LIGHTS_ETA_MIN": "3",
        "THL_TV_BACKEND": "samsung",
        "THL_TV_INPUT": "HDMI 1",
        "THL_CONSOLE_WOL": "1",
        "THL_CONSOLE_MACS": "11:22:33:44:55:66",
        "THL_CONSOLE_PLUG": "yes",
        "THL_PLUG_WEBHOOK": "http://x/plug",
        "THL_ALARM_WEBHOOK": "http://x/alarm",
        "THL_AI_MODE": "never",
        "THL_VEHICLES": "Model Y=VINY,Model 3=VIN3",
        "THL_DRY_RUN": "true",
        "THL_SERVER_PORT": "8650",
    })
    assert s.home_name == "Casa"
    assert s.home_lat == 34.5 and s.home_lon == -118.1
    assert s.thermostat_backend == "webhook"
    assert s.target_temp_f == 72.0
    assert s.hvac_eta_min == 30
    assert s.lights_backend == "hue"
    assert s.lights_eta_min == 3
    assert s.tv_backend == "samsung"
    assert s.console_wol is True
    assert s.console_macs == "11:22:33:44:55:66"
    assert s.console_plug is True
    assert s.ai_mode == "never"
    assert [(v.name, v.vin) for v in s.vehicles] == [("Model Y", "VINY"), ("Model 3", "VIN3")]
    assert s.dry_run is True
    assert s.server_port == 8650


def test_bad_thermostat_backend_rejected():
    with pytest.raises(ConfigError):
        load_settings({"THL_THERMOSTAT_BACKEND": "infrared"})


def test_bad_lights_backend_rejected():
    with pytest.raises(ConfigError):
        load_settings({"THL_LIGHTS_BACKEND": "zigbee"})


def test_bad_tv_backend_rejected():
    with pytest.raises(ConfigError):
        load_settings({"THL_TV_BACKEND": "roku"})


def test_zero_hvac_eta_rejected():
    with pytest.raises(ConfigError):
        load_settings({"THL_HVAC_ETA_MIN": "0"})


def test_bad_ai_mode_rejected():
    with pytest.raises(ConfigError):
        load_settings({"THL_AI_MODE": "sometimes"})


def test_bad_region_rejected():
    with pytest.raises(ConfigError):
        load_settings({"TESLA_REGION": "mars"})


def test_non_numeric_temp_rejected():
    with pytest.raises(ConfigError):
        load_settings({"THL_TARGET_TEMP_F": "cozy"})


def test_bare_vin_gets_generated_name():
    s = load_settings({"THL_VEHICLES": "VIN9"})
    assert s.vehicles[0].name == "car-1"
    assert s.vehicles[0].vin == "VIN9"
