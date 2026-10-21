"""Guided first-run setup: plain-language questions, writes .env itself.

Built for Tesla owners, not developers — defaults everywhere, numbered
choices, home given as name + street address (geocoded automatically),
temperatures in °F, times in plain minutes. Testable: input, output, and
the geocoder are injectable. Never shows a stack trace to the user.
"""
from __future__ import annotations

import os
import re
from pathlib import Path

import requests

NOMINATIM_URL = "https://nominatim.openstreetmap.org/search"

# Google Maps share links carry coordinates: "@34.5794,-118.1185,15z",
# "?q=34.5794,-118.1185", "/place/.../@34.5794,-118.1185".
_MAPS_COORD_RE = re.compile(
    r"(?:@|[?&](?:q|ll|daddr)=)(-?\d{1,3}\.\d+),(-?\d{1,3}\.\d+)")


def _ask(input_fn, out, prompt: str, default: str = "") -> str:
    suffix = f" [{default}]" if default else ""
    out(f"{prompt}{suffix}")
    answer = input_fn("> ").strip()
    return answer or default


def _ask_int(input_fn, out, prompt: str, default: int) -> int:
    while True:
        raw = _ask(input_fn, out, prompt, str(default))
        try:
            return int(raw)
        except ValueError:
            out(f"Please type a whole number (like {default}) — or press Enter to keep {default}.")


def _ask_float(input_fn, out, prompt: str, default: float) -> float:
    while True:
        raw = _ask(input_fn, out, prompt, str(default))
        try:
            return float(raw)
        except ValueError:
            out(f"Please type a number (like {default}) — or press Enter to keep {default}.")


def _ask_required(input_fn, out, prompt: str, help_text: str) -> str:
    while True:
        out(help_text)
        answer = input_fn(f"{prompt} ").strip()
        if answer:
            return answer
        out("That one's required — without it this step can't finish.")


def _ask_choice(input_fn, out, prompt: str, options: list[str], default: int) -> int:
    """Numbered choice; returns the 1-based index."""
    out(prompt)
    for i, opt in enumerate(options, 1):
        out(f"  {i}) {opt}")
    while True:
        raw = _ask(input_fn, out, f"Pick 1–{len(options)}:", str(default))
        try:
            n = int(raw)
        except ValueError:
            n = 0
        if 1 <= n <= len(options):
            return n
        out(f"Please pick a number from 1 to {len(options)}.")


def parse_maps_link(link: str) -> tuple[float, float] | None:
    """Pull (lat, lon) out of a pasted Google Maps link. None if not found."""
    m = _MAPS_COORD_RE.search(link)
    if not m:
        return None
    try:
        return float(m.group(1)), float(m.group(2))
    except ValueError:
        return None


class GeocodeError(Exception):
    pass


def geocode_nominatim(address: str, timeout: float = 12.0) -> list[tuple[str, float, float]]:
    """Look up a street address; returns [(display_name, lat, lon), ...].

    Raises GeocodeError (never a raw exception) with plain-words detail.
    """
    try:
        resp = requests.get(NOMINATIM_URL,
                            params={"q": address, "format": "json", "limit": 5,
                                    "addressdetails": 0},
                            headers={"User-Agent": "tesla-home-link/0.1.0"},
                            timeout=timeout)
    except requests.RequestException as exc:
        raise GeocodeError(
            "Couldn't reach the address lookup service — check your internet "
            "connection and try again.") from exc
    if resp.status_code != 200:
        raise GeocodeError(
            "The address lookup service isn't cooperating right now (it answered "
            "with an error). Wait a minute and try again.")
    try:
        results = resp.json()
    except ValueError as exc:
        raise GeocodeError(
            "The address lookup service gave a garbled answer. Try again in a bit.") from exc
    out_list = []
    for item in results or []:
        try:
            out_list.append((str(item["display_name"]),
                             float(item["lat"]), float(item["lon"])))
        except (KeyError, TypeError, ValueError):
            continue
    return out_list


def _resolve_home(input_fn, out, geocode_fn, address: str) -> tuple[str, float, float] | None:
    """Turn an address into a confirmed (display_name, lat, lon).

    Returns None if the user gives up; handles every failure in plain words.
    """
    if geocode_fn is None:
        geocode_fn = geocode_nominatim
    try:
        matches = geocode_fn(address)
    except GeocodeError as exc:
        out(f"Hmm — {exc}")
        out("Other ways to find your home:")
        out("  1) Try the address again (maybe with the city spelled out)")
        out("  2) Paste a Google Maps link to your home (open it in your "
            "browser and copy the address-bar URL)")
        choice = _ask(input_fn, out, "Pick 1 or 2 — or press Enter to skip for now:", "")
        if choice == "1":
            return _resolve_home(input_fn, out, geocode_fn,
                                 _ask_required(input_fn, out, "Address:",
                                               "Type the street address again:"))
        if choice == "2":
            link = _ask_required(input_fn, out, "Maps link:",
                                 "Paste the Google Maps link:")
            coords = parse_maps_link(link)
            if coords is None:
                out("I couldn't find coordinates in that link — make sure you "
                    "copied the full address-bar URL.")
                return _resolve_home(input_fn, out, geocode_fn, address)
            return (address, coords[0], coords[1])
        out("Skipping — you can fill in your home address later in .env "
            "(THL_HOME_ADDRESS / THL_HOME_LAT / THL_HOME_LON).")
        return None
    if not matches:
        out(f"I couldn't find \"{address}\" — double-check the spelling or add the city.")
        again = _ask(input_fn, out, "Address (or press Enter to skip):", "")
        if not again:
            return None
        return _resolve_home(input_fn, out, geocode_fn, again)
    if len(matches) == 1:
        name, lat, lon = matches[0]
        confirm = _ask(input_fn, out, f"Found: {name}. Is that right? (y/n)", "y")
        if confirm.lower() in ("y", "yes"):
            return name, lat, lon
        out("No problem — try the address again with more detail.")
        return _resolve_home(
            input_fn, out, geocode_fn,
            _ask_required(input_fn, out, "Address:", "Type the street address again:"))
    out("A few places match — pick one:")
    for i, (name, _lat, _lon) in enumerate(matches, 1):
        out(f"  {i}) {name}")
    out(f"  {len(matches) + 1}) None of these — try a different address")
    while True:
        choice = _ask(input_fn, out, f"Pick 1–{len(matches) + 1}:", "1")
        try:
            n = int(choice)
        except ValueError:
            n = 0
        if 1 <= n <= len(matches):
            return matches[n - 1]
        if n == len(matches) + 1:
            new_addr = _ask_required(input_fn, out, "Address:",
                                     "Type the street address again:")
            return _resolve_home(input_fn, out, geocode_fn, new_addr)
        out(f"Please pick a number from 1 to {len(matches) + 1}.")


def run_setup(input_fn=input, out=print, env_path: str = ".env",
              geocode_fn=None) -> Path:
    out("")
    out("Welcome to Tesla Home Link setup — I'll ask a few questions and")
    out("write the settings file for you. Press Enter to accept a [default].")
    out("Nothing you type leaves this computer.")
    out("")

    path = Path(env_path)
    if path.exists():
        again = _ask(input_fn, out,
                     "A settings file (.env) already exists. Replace it? (y/n)", "n")
        if again.lower() not in ("y", "yes"):
            out("Keeping your existing settings. Nothing changed.")
            return path

    cfg: dict[str, str] = {}

    # -- 1: Tesla ---------------------------------------------------------
    out("STEP 1 of 8 — Connect to Tesla (read-only)")
    out("This app only READS your car's location and navigation — it never")
    out("sends commands to the car. Create a free developer app at")
    out("developer.tesla.com (sign in, then 'Create App'). Tesla will show")
    out("you a Client ID and Client Secret.")
    cfg["TESLA_CLIENT_ID"] = _ask_required(input_fn, out, "Paste your Client ID:",
                                           "First, the Client ID:")
    cfg["TESLA_CLIENT_SECRET"] = _ask_required(input_fn, out, "Paste your Client Secret:",
                                               "Now the Client Secret (it stays on this computer):")

    out("")
    out("Your cars (I only need to know which ones to watch):")
    vehicles: list[tuple[str, str]] = []
    while True:
        n = len(vehicles) + 1
        name = _ask(input_fn, out,
                    f"Car {n} nickname (e.g. Model Y) — or press Enter if done:",
                    "" if vehicles else "")
        if not name:
            if vehicles:
                break
            out("Add at least one car to continue.")
            continue
        vin = _ask_required(input_fn, out, f"VIN for {name}:",
                            f"Now the VIN for {name} (in your Tesla app under "
                            "Controls → Software):")
        vehicles.append((name, vin))
    cfg["THL_VEHICLES"] = ",".join(f"{name}={vin}" for name, vin in vehicles)

    # -- 2: home location ---------------------------------------------------
    out("")
    out("STEP 2 of 8 — Your home")
    home_name = _ask(input_fn, out, "What do you call home? (used to match navigation)", "Home")
    address = _ask_required(input_fn, out, "Street address (house number, street, city):",
                            "Type your home's street address:")
    resolved = _resolve_home(input_fn, out, geocode_fn, address)
    if resolved is None:
        home_lat, home_lon, home_disp = "", "", ""
    else:
        home_disp, home_lat, home_lon = resolved[0], resolved[1], resolved[2]
    cfg["THL_HOME_NAME"] = home_name
    cfg["THL_HOME_ADDRESS"] = address
    cfg["THL_HOME_LAT"] = str(home_lat)
    cfg["THL_HOME_LON"] = str(home_lon)
    if home_disp:
        out(f"Got it: {home_disp.split(',')[0]}.")

    # -- 3: thermostat ------------------------------------------------------
    out("")
    out("STEP 3 of 8 — Thermostat (pre-heat / pre-cool before you arrive)")
    choice = _ask_choice(
        input_fn, out, "Which thermostat do you have?",
        ["No thermostat (skip — I'll just track and explain)",
         "Google Nest (Smart Device Management API)",
         "Ecobee",
         "Home Assistant (climate entity)",
         "Something else, via a webhook I call"],
        1)
    backend = ("none", "nest", "ecobee", "homeassistant", "webhook")[choice - 1]
    cfg["THL_THERMOSTAT_BACKEND"] = backend
    if backend == "nest":
        out("In the Google Cloud console, enable the Smart Device Management API,")
        out("then paste the project values here:")
        cfg["THL_NEST_PROJECT_ID"] = _ask_required(input_fn, out, "Project ID:", "Project ID:")
        cfg["THL_NEST_CLIENT_ID"] = _ask_required(input_fn, out, "OAuth client ID:", "OAuth client ID:")
        cfg["THL_NEST_CLIENT_SECRET"] = _ask_required(input_fn, out, "OAuth client secret:", "OAuth client secret:")
        cfg["THL_NEST_REFRESH_TOKEN"] = _ask_required(input_fn, out, "Refresh token:", "Refresh token:")
        cfg["THL_NEST_DEVICE_ID"] = _ask_required(input_fn, out, "Thermostat device ID:", "Thermostat device ID:")
    elif backend == "ecobee":
        out("Create an app at developer.ecobee.com (it's free), then:")
        cfg["THL_ECOBEE_API_KEY"] = _ask_required(input_fn, out, "App API key:", "App API key:")
        cfg["THL_ECOBEE_REFRESH_TOKEN"] = _ask_required(input_fn, out, "Refresh token:", "Refresh token:")
        cfg["THL_ECOBEE_THERMOSTAT_ID"] = _ask_required(input_fn, out, "Thermostat ID:", "Thermostat ID:")
    elif backend == "homeassistant":
        cfg["THL_HA_URL"] = _ask_required(input_fn, out, "Home Assistant address (e.g. http://homeassistant:8123):",
                                          "Your Home Assistant's address:")
        cfg["THL_HA_TOKEN"] = _ask_required(input_fn, out, "Long-lived access token (HA → your profile → Security):",
                                            "Long-lived access token:")
        cfg["THL_HA_CLIMATE_ENTITY"] = _ask(input_fn, out, "Climate entity name:", "climate.home")
    elif backend == "webhook":
        cfg["THL_THERMOSTAT_WEBHOOK"] = _ask_required(
            input_fn, out, "Webhook URL (I POST {\"target_f\": <temp>}):",
            "The URL I should POST {\"target_f\": <temp>} to:")
    if backend != "none":
        cfg["THL_TARGET_TEMP_F"] = str(_ask_float(
            input_fn, out, "What indoor temperature should feel like 'home'? (°F):", 71.0))
        cfg["THL_AWAY_SETBACK_F"] = str(_ask_float(
            input_fn, out, "And the energy-saving temp while you're away? (°F):", 60.0))
        cfg["THL_HVAC_ETA_MIN"] = str(_ask_int(
            input_fn, out, "How many minutes before arrival should the heat/AC "
                           "start? (houses are slow — 20 is a good start):", 20))
    else:
        cfg["THL_TARGET_TEMP_F"] = "71"
        cfg["THL_AWAY_SETBACK_F"] = "60"
        cfg["THL_HVAC_ETA_MIN"] = "20"

    # -- 4: lights -----------------------------------------------------------
    out("")
    out("STEP 4 of 8 — Porch / entry lights")
    choice = _ask_choice(
        input_fn, out, "Which lights should welcome you?",
        ["No lights (skip)",
         "Philips Hue (local bridge)",
         "Something else, via a webhook I call"],
        1)
    lights_backend = ("none", "hue", "webhook")[choice - 1]
    cfg["THL_LIGHTS_BACKEND"] = lights_backend
    if lights_backend == "hue":
        out("Find your bridge's IP in the Hue app under Settings → Hue Bridges.")
        cfg["THL_HUE_BRIDGE_IP"] = _ask_required(input_fn, out, "Bridge IP address:", "Bridge IP address:")
        out("Now press the round link button on the bridge, then paste an API")
        out("key — or leave blank and I'll use the default discovery flow later.")
        cfg["THL_HUE_API_KEY"] = _ask(input_fn, out, "Hue API key:")
        cfg["THL_HUE_SCENE"] = _ask(input_fn, out, "Hue scene to activate (optional — blank = just turn on):")
    elif lights_backend == "webhook":
        cfg["THL_LIGHTS_WEBHOOK"] = _ask_required(
            input_fn, out, "Webhook URL (I POST {\"action\": \"on\"/\"off\"}):",
            "The URL I should POST to:")
    if lights_backend != "none":
        cfg["THL_LIGHTS_ETA_MIN"] = str(_ask_int(
            input_fn, out, "Turn the lights on how many minutes before arrival?", 5))
        cfg["THL_LIGHTS_AUTO_OFF_MIN"] = str(_ask_int(
            input_fn, out, "Turn them back off how many minutes after arrival?", 15))
    else:
        cfg["THL_LIGHTS_ETA_MIN"] = "5"
        cfg["THL_LIGHTS_AUTO_OFF_MIN"] = "15"

    # -- 5: entertainment ------------------------------------------------------
    out("")
    out("STEP 5 of 8 — Welcome-home entertainment (optional)")
    out("Arrive home to the TV on, the right input, your console awake.")
    choice = _ask_choice(
        input_fn, out, "Which TV should I wake?",
        ["No TV (skip)", "Samsung", "LG"],
        1)
    tv_backend = ("none", "samsung", "lg")[choice - 1]
    cfg["THL_TV_BACKEND"] = tv_backend
    if tv_backend != "none":
        out("Find the TV's IP in its network settings (same Wi-Fi as this computer).")
        cfg["THL_TV_IP"] = _ask(input_fn, out, "TV IP address (blank to skip):")
        out("The MAC address lets me wake it from standby (TV → Settings → Network).")
        cfg["THL_TV_MAC"] = _ask(input_fn, out, "TV MAC address (blank to skip):")
        cfg["THL_TV_INPUT"] = _ask(input_fn, out, "Input to switch to (e.g. HDMI 1 — blank to skip):")
        cfg["THL_TV_APP"] = _ask(input_fn, out, "App to open (e.g. YouTube — blank to skip):")
    else:
        cfg["THL_TV_IP"] = cfg["THL_TV_MAC"] = cfg["THL_TV_INPUT"] = cfg["THL_TV_APP"] = ""

    want_console = _ask(input_fn, out, "Wake a game console from standby on approach? (y/n)", "n")
    cfg["THL_CONSOLE_WOL"] = "1" if want_console.lower() in ("y", "yes") else "0"
    if cfg["THL_CONSOLE_WOL"] == "1":
        out("MAC address(es), comma-separated (console → Settings → Network).")
        cfg["THL_CONSOLE_MACS"] = _ask_required(input_fn, out, "Console MAC address(es):",
                                                "Type the MAC address(es):")
    else:
        cfg["THL_CONSOLE_MACS"] = ""
    want_plug = _ask(input_fn, out, "Restore power to a console smart plug on approach? (y/n)", "n")
    cfg["THL_CONSOLE_PLUG"] = "1" if want_plug.lower() in ("y", "yes") else "0"
    if cfg["THL_CONSOLE_PLUG"] == "1":
        cfg["THL_PLUG_WEBHOOK"] = _ask_required(
            input_fn, out, "Plug webhook URL (I POST {\"action\": \"on\"}):",
            "The URL that turns the plug on:")
    else:
        cfg["THL_PLUG_WEBHOOK"] = ""

    # -- 6: leave-home ---------------------------------------------------------
    out("")
    out("STEP 6 of 8 — Leave-home (optional)")
    out("When you drive away I'll set the thermostat back and kill the lights.")
    want_alarm = _ask(input_fn, out, "Also arm an alarm via webhook on departure? (y/n)", "n")
    cfg["THL_ALARM_WEBHOOK"] = ""
    if want_alarm.lower() in ("y", "yes"):
        cfg["THL_ALARM_WEBHOOK"] = _ask_required(
            input_fn, out, "Alarm webhook URL (I POST {\"action\": \"arm\"}):",
            "The URL that arms the alarm:")

    # -- 7: AI -----------------------------------------------------------------
    out("")
    out("STEP 7 of 8 — The AI brain (optional)")
    out("  1) Built-in answers only — no AI, nothing leaves this computer")
    out("  2) Ollama on this computer — free local AI (default)")
    out("  3) Another AI service (OpenAI, Groq, LM Studio…) — you'll paste its details")
    choice = _ask(input_fn, out, "Pick 1, 2 or 3:", "2")
    ai_mode, ai_url, ai_key, ai_model = "auto", "http://localhost:11434/v1", "", "llama3.1"
    if choice == "1":
        ai_mode = "never"
    elif choice == "3":
        ai_url = _ask_required(input_fn, out, "Service address (base URL):",
                               "Paste the service's API address:")
        ai_key = _ask(input_fn, out, "API key for that service (stored only here):")
        ai_model = _ask(input_fn, out, "Model name:", "gpt-4o-mini")
    cfg["THL_AI_MODE"] = ai_mode
    cfg["THL_AI_BASE_URL"] = ai_url
    cfg["THL_AI_API_KEY"] = ai_key
    cfg["THL_AI_MODEL"] = ai_model

    # -- 8: server --------------------------------------------------------------
    out("")
    out("STEP 8 of 8 — The chat server")
    cfg["THL_SERVER_PORT"] = _ask(input_fn, out, "Port for the chat address:", "8644")
    cfg["THL_SERVER_API_KEY"] = _ask(input_fn, out,
                                     "Optional password for the chat address (blank = none):")

    lines = [
        "# Written by `thlink setup` — edit by re-running setup any time.",
        f"TESLA_CLIENT_ID={cfg['TESLA_CLIENT_ID']}",
        f"TESLA_CLIENT_SECRET={cfg['TESLA_CLIENT_SECRET']}",
        "TESLA_REGION=na",
        "TESLA_REDIRECT_URI=http://localhost:8080/callback",
        f"THL_VEHICLES={cfg['THL_VEHICLES']}",
        f"THL_HOME_NAME={cfg['THL_HOME_NAME']}",
        f"THL_HOME_ADDRESS={cfg['THL_HOME_ADDRESS']}",
        f"THL_HOME_LAT={cfg['THL_HOME_LAT']}",
        f"THL_HOME_LON={cfg['THL_HOME_LON']}",
        "THL_ARRIVAL_RADIUS_M=150",
        "THL_APPROACH_GEOFENCE_M=12000",
        f"THL_THERMOSTAT_BACKEND={cfg['THL_THERMOSTAT_BACKEND']}",
        f"THL_TARGET_TEMP_F={cfg['THL_TARGET_TEMP_F']}",
        f"THL_AWAY_SETBACK_F={cfg['THL_AWAY_SETBACK_F']}",
        f"THL_HVAC_ETA_MIN={cfg['THL_HVAC_ETA_MIN']}",
        f"THL_NEST_PROJECT_ID={cfg.get('THL_NEST_PROJECT_ID', '')}",
        f"THL_NEST_CLIENT_ID={cfg.get('THL_NEST_CLIENT_ID', '')}",
        f"THL_NEST_CLIENT_SECRET={cfg.get('THL_NEST_CLIENT_SECRET', '')}",
        f"THL_NEST_REFRESH_TOKEN={cfg.get('THL_NEST_REFRESH_TOKEN', '')}",
        f"THL_NEST_DEVICE_ID={cfg.get('THL_NEST_DEVICE_ID', '')}",
        f"THL_ECOBEE_API_KEY={cfg.get('THL_ECOBEE_API_KEY', '')}",
        f"THL_ECOBEE_REFRESH_TOKEN={cfg.get('THL_ECOBEE_REFRESH_TOKEN', '')}",
        f"THL_ECOBEE_THERMOSTAT_ID={cfg.get('THL_ECOBEE_THERMOSTAT_ID', '')}",
        f"THL_HA_URL={cfg.get('THL_HA_URL', '')}",
        f"THL_HA_TOKEN={cfg.get('THL_HA_TOKEN', '')}",
        f"THL_HA_CLIMATE_ENTITY={cfg.get('THL_HA_CLIMATE_ENTITY', '')}",
        f"THL_THERMOSTAT_WEBHOOK={cfg.get('THL_THERMOSTAT_WEBHOOK', '')}",
        f"THL_LIGHTS_BACKEND={cfg['THL_LIGHTS_BACKEND']}",
        f"THL_LIGHTS_ETA_MIN={cfg['THL_LIGHTS_ETA_MIN']}",
        f"THL_LIGHTS_AUTO_OFF_MIN={cfg['THL_LIGHTS_AUTO_OFF_MIN']}",
        f"THL_HUE_BRIDGE_IP={cfg.get('THL_HUE_BRIDGE_IP', '')}",
        f"THL_HUE_API_KEY={cfg.get('THL_HUE_API_KEY', '')}",
        f"THL_HUE_SCENE={cfg.get('THL_HUE_SCENE', '')}",
        f"THL_LIGHTS_WEBHOOK={cfg.get('THL_LIGHTS_WEBHOOK', '')}",
        f"THL_TV_BACKEND={cfg['THL_TV_BACKEND']}",
        f"THL_TV_IP={cfg['THL_TV_IP']}",
        f"THL_TV_MAC={cfg['THL_TV_MAC']}",
        f"THL_TV_INPUT={cfg['THL_TV_INPUT']}",
        f"THL_TV_APP={cfg['THL_TV_APP']}",
        f"THL_CONSOLE_WOL={cfg['THL_CONSOLE_WOL']}",
        f"THL_CONSOLE_MACS={cfg['THL_CONSOLE_MACS']}",
        f"THL_CONSOLE_PLUG={cfg['THL_CONSOLE_PLUG']}",
        f"THL_PLUG_WEBHOOK={cfg['THL_PLUG_WEBHOOK']}",
        f"THL_ALARM_WEBHOOK={cfg['THL_ALARM_WEBHOOK']}",
        "THL_SERVER_HOST=127.0.0.1",
        f"THL_SERVER_PORT={cfg['THL_SERVER_PORT']}",
        f"THL_SERVER_API_KEY={cfg['THL_SERVER_API_KEY']}",
        "THL_MODEL_ID=tesla-home-link",
        f"THL_AI_BASE_URL={cfg['THL_AI_BASE_URL']}",
        f"THL_AI_API_KEY={cfg['THL_AI_API_KEY']}",
        f"THL_AI_MODEL={cfg['THL_AI_MODEL']}",
        f"THL_AI_MODE={cfg['THL_AI_MODE']}",
        "THL_AI_TIMEOUT_SECONDS=20",
        "THL_POLL_SECONDS=120",
        "THL_TOKEN_FILE=~/.thlink/tokens.json",
        "THL_STATE_FILE=~/.thlink/state.json",
        "THL_DRY_RUN=0",
    ]
    path.write_text("\n".join(lines) + "\n")
    os.chmod(path, 0o600)

    out("")
    out("All set — settings saved to .env (only readable by you).")
    out("Next steps:")
    out("  1) thlink auth       — sign in to Tesla once (opens a link)")
    out("  2) thlink simulate   — watch a pretend evening drive home")
    out("  3) thlink run --dry-run — preview what would happen with your real home")
    out("  4) thlink serve      — start the chat server (ask 'why did the heat start?')")
    return path
