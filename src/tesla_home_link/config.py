"""Configuration for Tesla Home Link. All settings come from the
environment (see .env.example). Copy .env.example to .env — .env is
git-ignored so secrets never leave your machine.

All variables are prefixed THL_ (tesla-home-link).
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field


class ConfigError(Exception):
    pass


@dataclass
class VehicleConfig:
    name: str
    vin: str


@dataclass
class Settings:
    # -- home identity --------------------------------------------------
    home_name: str = "Home"
    home_address: str = ""
    home_lat: float = 0.0
    home_lon: float = 0.0
    arrival_radius_m: float = 150.0        # inside this = "home"
    approach_geofence_m: float = 12000.0   # fallback detector outer bound (~7.5 mi)
    # -- thermostat ------------------------------------------------------
    thermostat_backend: str = "none"       # nest | ecobee | homeassistant | webhook | none
    target_temp_f: float = 71.0            # comfort temp when you're on your way home
    away_setback_f: float = 60.0           # away-mode setpoint (leave-home trigger)
    hvac_eta_min: int = 20                 # fire pre-heat/cool at or below this ETA
    nest_project_id: str = ""
    nest_client_id: str = ""
    nest_client_secret: str = ""
    nest_refresh_token: str = ""
    nest_device_id: str = ""
    ecobee_api_key: str = ""
    ecobee_refresh_token: str = ""
    ecobee_thermostat_id: str = ""
    ha_url: str = ""
    ha_token: str = ""
    ha_climate_entity: str = ""
    thermostat_webhook: str = ""           # generic: POST {"target_f": <n>}
    # -- lights ----------------------------------------------------------
    lights_backend: str = "none"           # hue | webhook | none
    lights_eta_min: int = 5
    lights_auto_off_min: int = 15          # turn off N min after arrival
    hue_bridge_ip: str = ""
    hue_api_key: str = ""
    hue_scene: str = ""                    # optional Hue scene name to activate
    lights_webhook: str = ""               # generic: POST {"action": "on"|"off"}
    # -- welcome-home entertainment ---------------------------------------
    tv_backend: str = "none"               # samsung | lg | none
    tv_ip: str = ""
    tv_mac: str = ""                       # for Wake-on-LAN
    tv_input: str = ""                     # e.g. "HDMI 1"
    tv_app: str = ""                       # e.g. "YouTube"
    console_wol: bool = False              # Wake-on-LAN for standby consoles (PS5)
    console_macs: str = ""                 # comma-separated MAC addresses
    console_plug: bool = False             # smart-plug power restore (e.g. Xbox 360)
    plug_webhook: str = ""                 # POST {"action": "on"} to restore power
    # -- leave-home -------------------------------------------------------
    alarm_webhook: str = ""                # optional: POST {"action": "arm"} on departure
    # -- the OpenAI-compatible server this app exposes -------------------
    server_host: str = "127.0.0.1"
    server_port: int = 8644
    server_api_key: str = ""               # optional bearer token for /v1/*
    model_id: str = "tesla-home-link"
    # -- pluggable LLM backend (any OpenAI-compatible API; default: local Ollama)
    ai_base_url: str = "http://localhost:11434/v1"
    ai_api_key: str = ""
    ai_model: str = "llama3.1"
    ai_mode: str = "auto"                  # auto | always | never
    ai_timeout_seconds: float = 20.0
    # -- runtime ----------------------------------------------------------
    poll_seconds: int = 120                # how often `run` re-checks (via cron)
    state_file: str = "~/.thlink/state.json"
    token_file: str = "~/.thlink/tokens.json"
    vehicles: list[VehicleConfig] = field(default_factory=list)
    tesla_client_id: str = ""
    tesla_client_secret: str = ""
    tesla_region: str = "na"
    tesla_redirect_uri: str = "http://localhost:8080/callback"
    dry_run: bool = False


def _int(env: dict, key: str, default: int) -> int:
    raw = env.get(key)
    if raw is None or not str(raw).strip():
        return default
    try:
        return int(raw)
    except (ValueError, TypeError) as exc:
        raise ConfigError(f"{key} must be a whole number, got {raw!r}") from exc


def _float(env: dict, key: str, default: float) -> float:
    raw = env.get(key)
    if raw is None or not str(raw).strip():
        return default
    try:
        return float(raw)
    except (ValueError, TypeError) as exc:
        raise ConfigError(f"{key} must be a number, got {raw!r}") from exc


def _bool(env: dict, key: str, default: bool = False) -> bool:
    raw = env.get(key)
    if raw is None:
        return default
    return raw.strip().lower() in ("1", "true", "yes", "on")


def _choice(env: dict, key: str, default: str, allowed: tuple[str, ...]) -> str:
    value = env.get(key, default).strip().lower()
    if value not in allowed:
        raise ConfigError(f"{key} must be one of {', '.join(allowed)}, got {value!r}")
    return value


def load_settings(env: dict | None = None) -> Settings:
    env = os.environ if env is None else env
    s = Settings()
    s.home_name = env.get("THL_HOME_NAME", s.home_name).strip() or s.home_name
    s.home_address = env.get("THL_HOME_ADDRESS", "").strip()
    s.home_lat = _float(env, "THL_HOME_LAT", s.home_lat)
    s.home_lon = _float(env, "THL_HOME_LON", s.home_lon)
    s.arrival_radius_m = _float(env, "THL_ARRIVAL_RADIUS_M", s.arrival_radius_m)
    s.approach_geofence_m = _float(env, "THL_APPROACH_GEOFENCE_M", s.approach_geofence_m)
    s.thermostat_backend = _choice(env, "THL_THERMOSTAT_BACKEND", s.thermostat_backend,
                                   ("nest", "ecobee", "homeassistant", "webhook", "none"))
    s.target_temp_f = _float(env, "THL_TARGET_TEMP_F", s.target_temp_f)
    s.away_setback_f = _float(env, "THL_AWAY_SETBACK_F", s.away_setback_f)
    s.hvac_eta_min = _int(env, "THL_HVAC_ETA_MIN", s.hvac_eta_min)
    if s.hvac_eta_min < 1:
        raise ConfigError("THL_HVAC_ETA_MIN must be at least 1 minute")
    s.nest_project_id = env.get("THL_NEST_PROJECT_ID", "")
    s.nest_client_id = env.get("THL_NEST_CLIENT_ID", "")
    s.nest_client_secret = env.get("THL_NEST_CLIENT_SECRET", "")
    s.nest_refresh_token = env.get("THL_NEST_REFRESH_TOKEN", "")
    s.nest_device_id = env.get("THL_NEST_DEVICE_ID", "")
    s.ecobee_api_key = env.get("THL_ECOBEE_API_KEY", "")
    s.ecobee_refresh_token = env.get("THL_ECOBEE_REFRESH_TOKEN", "")
    s.ecobee_thermostat_id = env.get("THL_ECOBEE_THERMOSTAT_ID", "")
    s.ha_url = env.get("THL_HA_URL", "").rstrip("/")
    s.ha_token = env.get("THL_HA_TOKEN", "")
    s.ha_climate_entity = env.get("THL_HA_CLIMATE_ENTITY", "")
    s.thermostat_webhook = env.get("THL_THERMOSTAT_WEBHOOK", "")
    s.lights_backend = _choice(env, "THL_LIGHTS_BACKEND", s.lights_backend,
                               ("hue", "webhook", "none"))
    s.lights_eta_min = _int(env, "THL_LIGHTS_ETA_MIN", s.lights_eta_min)
    if s.lights_eta_min < 1:
        raise ConfigError("THL_LIGHTS_ETA_MIN must be at least 1 minute")
    s.lights_auto_off_min = _int(env, "THL_LIGHTS_AUTO_OFF_MIN", s.lights_auto_off_min)
    s.hue_bridge_ip = env.get("THL_HUE_BRIDGE_IP", "")
    s.hue_api_key = env.get("THL_HUE_API_KEY", "")
    s.hue_scene = env.get("THL_HUE_SCENE", "")
    s.lights_webhook = env.get("THL_LIGHTS_WEBHOOK", "")
    s.tv_backend = _choice(env, "THL_TV_BACKEND", s.tv_backend,
                           ("samsung", "lg", "none"))
    s.tv_ip = env.get("THL_TV_IP", "")
    s.tv_mac = env.get("THL_TV_MAC", "")
    s.tv_input = env.get("THL_TV_INPUT", "")
    s.tv_app = env.get("THL_TV_APP", "")
    s.console_wol = _bool(env, "THL_CONSOLE_WOL")
    s.console_macs = env.get("THL_CONSOLE_MACS", "")
    s.console_plug = _bool(env, "THL_CONSOLE_PLUG")
    s.plug_webhook = env.get("THL_PLUG_WEBHOOK", "")
    s.alarm_webhook = env.get("THL_ALARM_WEBHOOK", "")
    s.server_host = env.get("THL_SERVER_HOST", s.server_host)
    s.server_port = _int(env, "THL_SERVER_PORT", s.server_port)
    s.server_api_key = env.get("THL_SERVER_API_KEY", "")
    s.model_id = env.get("THL_MODEL_ID", s.model_id)
    s.ai_base_url = env.get("THL_AI_BASE_URL", s.ai_base_url).rstrip("/")
    s.ai_api_key = env.get("THL_AI_API_KEY", "")
    s.ai_model = env.get("THL_AI_MODEL", s.ai_model)
    s.ai_mode = _choice(env, "THL_AI_MODE", s.ai_mode, ("auto", "always", "never"))
    s.ai_timeout_seconds = _float(env, "THL_AI_TIMEOUT_SECONDS", s.ai_timeout_seconds)
    s.poll_seconds = _int(env, "THL_POLL_SECONDS", s.poll_seconds)
    s.state_file = env.get("THL_STATE_FILE", s.state_file)
    s.token_file = env.get("THL_TOKEN_FILE", s.token_file)
    s.tesla_client_id = env.get("TESLA_CLIENT_ID", "")
    s.tesla_client_secret = env.get("TESLA_CLIENT_SECRET", "")
    s.tesla_region = env.get("TESLA_REGION", "na").strip().lower()
    if s.tesla_region not in ("na", "eu", "cn"):
        raise ConfigError(f"TESLA_REGION must be na|eu|cn, got {s.tesla_region!r}")
    s.tesla_redirect_uri = env.get("TESLA_REDIRECT_URI", s.tesla_redirect_uri)
    raw_vehicles = env.get("THL_VEHICLES", "")
    if raw_vehicles.strip():
        s.vehicles = []
        for idx, item in enumerate(raw_vehicles.split(",")):
            item = item.strip()
            if not item:
                continue
            name = None
            if "=" in item:
                name, item = item.split("=", 1)
                name = name.strip()
            if not item:
                raise ConfigError("Empty VIN in THL_VEHICLES")
            s.vehicles.append(VehicleConfig(name=name or f"car-{idx + 1}", vin=item))
        if not s.vehicles:
            raise ConfigError("THL_VEHICLES is empty — add at least one name=VIN entry")
    s.dry_run = _bool(env, "THL_DRY_RUN")
    return s
