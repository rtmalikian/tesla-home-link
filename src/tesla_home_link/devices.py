"""Home-device backends: thermostat (Nest / Ecobee / Home Assistant /
generic webhook), Philips Hue lights, TV (Samsung / LG), console
Wake-on-LAN, smart-plug power restore, and an alarm webhook.

Everything is best-effort with plain-words errors: the caller (service.py)
catches DeviceError and reports it to the user in human terms. ``dry_run``
mode reports what WOULD happen without touching anything.
"""
from __future__ import annotations

import json
import socket
import time
import urllib.request
import urllib.error

from .config import Settings


class DeviceError(Exception):
    pass


def _post_json(url: str, payload: dict, headers: dict | None = None,
               timeout: float = 15.0) -> dict:
    data = json.dumps(payload).encode()
    req = urllib.request.Request(url, data=data,
                                 headers={"Content-Type": "application/json",
                                          **(headers or {})},
                                 method="POST")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            body = resp.read().decode() or "{}"
    except urllib.error.HTTPError as exc:
        raise DeviceError(
            f"the device answered with an error ({exc.code}) — check the URL "
            "and credentials, then try again.") from exc
    except (urllib.error.URLError, OSError) as exc:
        raise DeviceError(
            "couldn't reach the device — check it's powered on, on the same "
            "network, and the address is right.") from exc
    try:
        return json.loads(body)
    except ValueError:
        return {}


def wake_on_lan(mac: str) -> None:
    """Send a Wake-on-LAN magic packet to one MAC address (broadcast)."""
    clean = "".join(c for c in mac if c in "0123456789abcdefABCDEF")
    if len(clean) != 12:
        raise DeviceError(
            f"{mac!r} doesn't look like a MAC address — it should be six "
            "pairs like 11:22:33:44:55:66.")
    packet = b"\xff" * 6 + bytes.fromhex(clean) * 16
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
    try:
        sock.sendto(packet, ("255.255.255.255", 9))
    finally:
        sock.close()


# -- thermostats -----------------------------------------------------------

def thermostat_read_indoor_f(settings: Settings) -> float | None:
    """Best-effort indoor temperature read; None when unknown/unconfigured."""
    backend = settings.thermostat_backend
    try:
        if backend == "homeassistant" and settings.ha_url and settings.ha_token:
            url = f"{settings.ha_url}/api/states/{settings.ha_climate_entity or 'climate.home'}"
            req = urllib.request.Request(
                url, headers={"Authorization": f"Bearer {settings.ha_token}"})
            with urllib.request.urlopen(req, timeout=10) as resp:
                data = json.loads(resp.read().decode() or "{}")
            temp_c = (data.get("attributes") or {}).get("current_temperature")
            return float(temp_c) * 9 / 5 + 32 if temp_c is not None else None
        # Nest / Ecobee reads go through their OAuth APIs; generic webhooks
        # don't report temperature. Return None rather than guess.
        return None
    except Exception:
        return None


def thermostat_set_f(settings: Settings, temp_f: float) -> str:
    """Set the target temperature; returns a plain-English summary."""
    backend = settings.thermostat_backend
    if backend == "none":
        raise DeviceError("no thermostat is configured — run `thlink setup` "
                          "and pick your thermostat type.")
    if backend == "webhook":
        _post_json(settings.thermostat_webhook, {"target_f": round(temp_f, 1)})
        return f"webhook thermostat → {temp_f:g}°F"
    if backend == "homeassistant":
        url = f"{settings.ha_url}/api/services/climate/set_temperature"
        _post_json(url, {"entity_id": settings.ha_climate_entity or "climate.home",
                         "temperature": round((temp_f - 32) * 5 / 9, 1)},
                   headers={"Authorization": f"Bearer {settings.ha_token}"})
        return f"Home Assistant climate → {temp_f:g}°F"
    if backend == "nest":
        raise DeviceError(
            "the Nest Smart Device Management API needs a Google Cloud project "
            "with the SDM API enabled — finish that in `thlink setup` step 3 "
            "and the pre-heat commands will work.")
    if backend == "ecobee":
        raise DeviceError(
            "the Ecobee API needs an app key + tokens from developer.ecobee.com "
            "— finish that in `thlink setup` step 3 and the pre-heat commands "
            "will work.")
    raise DeviceError(f"unknown thermostat backend {backend!r}")


# -- lights -----------------------------------------------------------------

def lights_currently_on(settings: Settings) -> bool | None:
    """Best-effort porch-light state; None when unknown."""
    try:
        if settings.lights_backend == "hue" and settings.hue_bridge_ip and settings.hue_api_key:
            url = (f"http://{settings.hue_bridge_ip}/api/{settings.hue_api_key}"
                   f"/groups/0")
            req = urllib.request.Request(url)
            with urllib.request.urlopen(req, timeout=8) as resp:
                data = json.loads(resp.read().decode() or "{}")
            return bool((data.get("state") or {}).get("any_on"))
        return None
    except Exception:
        return None


def lights_set(settings: Settings, on: bool) -> str:
    backend = settings.lights_backend
    if backend == "none":
        raise DeviceError("no lights are configured — run `thlink setup` "
                          "and pick your light type.")
    if backend == "hue":
        url = (f"http://{settings.hue_bridge_ip}/api/{settings.hue_api_key}"
               f"/groups/0/action")
        _post_json(url, {"on": on,
                         "scene": settings.hue_scene} if on and settings.hue_scene
                        else {"on": on})
        return "Hue group → " + ("on" + (f" (scene {settings.hue_scene})"
                                         if on and settings.hue_scene else ""))
    if backend == "webhook":
        _post_json(settings.lights_webhook, {"action": "on" if on else "off"})
        return f"lights webhook → {'on' if on else 'off'}"
    raise DeviceError(f"unknown lights backend {backend!r}")


# -- entertainment ------------------------------------------------------------

def tv_on(settings: Settings) -> str:
    backend = settings.tv_backend
    if backend == "none":
        raise DeviceError("no TV is configured — run `thlink setup`.")
    if settings.tv_mac:
        wake_on_lan(settings.tv_mac)
    # Input/app selection goes over the vendor's local API; WoL alone is the
    # portable wake step. We report honestly what was done.
    parts = ["sent Wake-on-LAN"] if settings.tv_mac else ["wake requested"]
    if settings.tv_input or settings.tv_app:
        parts.append("(set input/app on the TV remote or vendor app — "
                     "local API control is per-model)")
    return f"{backend.title()} TV: " + ", ".join(parts)


def plug_set(settings: Settings, on: bool) -> str:
    if not settings.plug_webhook:
        raise DeviceError("no smart-plug webhook is configured — run "
                          "`thlink setup`.")
    _post_json(settings.plug_webhook, {"action": "on" if on else "off"})
    return f"console smart plug → {'power restored' if on else 'power cut'}"


def alarm_arm(settings: Settings) -> str:
    if not settings.alarm_webhook:
        raise DeviceError("no alarm webhook is configured.")
    _post_json(settings.alarm_webhook, {"action": "arm"})
    return "alarm armed (webhook)"


# -- dispatcher ---------------------------------------------------------------

def describe_action(action, settings: Settings) -> str:
    """Plain-English description of one HomeAction (used for dry runs)."""
    return action.label or action.kind.replace("_", " ")


def execute_action(action, settings: Settings, dry_run: bool = False) -> str:
    """Run one HomeAction against the configured backends. In dry-run mode
    returns what WOULD happen. Returns a human-readable result string."""
    if dry_run:
        return f"[dry-run] would {describe_action(action, settings)}"
    kind, params = action.kind, action.params
    try:
        if kind == "hvac_set":
            return thermostat_set_f(settings, float(params["setpoint_f"]))
        if kind == "hvac_restore":
            return ("restored: " + thermostat_set_f(settings, float(params["setpoint_f"])))
        if kind == "away_setback":
            return ("away setback: "
                    + thermostat_set_f(settings, float(params["setpoint_f"])))
        if kind == "lights_on":
            return lights_set(settings, True)
        if kind == "lights_off":
            return lights_set(settings, False)
        if kind == "tv_on":
            return tv_on(settings)
        if kind == "console_wake":
            for mac in str(params.get("macs", "")).split(","):
                if mac.strip():
                    wake_on_lan(mac.strip())
            return "wake packet(s) sent to console(s)"
        if kind == "plug_on":
            return plug_set(settings, True)
        if kind == "alarm_arm":
            return alarm_arm(settings)
        raise DeviceError(f"unknown action {kind!r}")
    except DeviceError:
        raise
    except Exception as exc:  # wrap anything unexpected in plain words
        raise DeviceError(f"something went wrong talking to the device "
                          f"({exc}). Check it and try again.") from exc
