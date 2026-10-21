"""The setup wizard must produce a config the app can load — no developer
knowledge required from the (scripted) user."""
from tesla_home_link.config import load_settings
from tesla_home_link.setup import parse_maps_link, run_setup


def _scripted(answers):
    it = iter(answers)
    def fn(prompt=""):
        try:
            return next(it)
        except StopIteration:
            raise AssertionError(f"wizard asked for more input than scripted "
                                 f"(prompt: {prompt!r})")
    return fn


def _fake_geocode(matches):
    def fn(address, timeout=12.0):
        return matches
    return fn


def _parse_env(path):
    env = {}
    for line in path.read_text().splitlines():
        if "=" in line and not line.startswith("#"):
            k, v = line.split("=", 1)
            env[k] = v
    return env


GEO = [("123 Main St, Palmdale, CA, USA", 34.5794, -118.1185)]

TAIL = ["n",      # console wake
        "n",      # smart plug
        "n",      # alarm webhook
        "1",      # AI: built-in only
        "",       # server port default
        "",       # no password
        ]


def test_wizard_full_flow_writes_loadable_env(tmp_path):
    answers = [
        "cid-123", "secret-xyz",              # step 1: Tesla credentials
        "Model Y", "VINY", "",                # one car, then done
        "",                                   # step 2: home name default
        "123 Main St, Palmdale, CA",          # home address
        "y",                                  # confirm geocode match
        "5",                                  # step 3: thermostat = webhook
        "http://x/thermo",                    # webhook URL
        "72",                                 # comfort temp
        "58",                                 # away setback
        "25",                                 # HVAC at 25 min out
        "2",                                  # step 4: lights = Hue
        "192.168.1.5",                        # bridge IP
        "",                                   # API key blank
        "",                                   # scene blank
        "4",                                  # lights at 4 min out
        "10",                                 # auto-off after 10 min
        "2",                                  # step 5: TV = Samsung
        "", "", "HDMI 1", "",                 # TV ip/mac blank, input, app blank
        *TAIL,
    ]
    env_file = tmp_path / ".env"
    run_setup(input_fn=_scripted(answers), out=lambda *a: None,
              env_path=str(env_file), geocode_fn=_fake_geocode(GEO))
    settings = load_settings(_parse_env(env_file))
    assert settings.home_name == "Home"
    assert settings.home_lat == 34.5794
    assert settings.thermostat_backend == "webhook"
    assert settings.thermostat_webhook == "http://x/thermo"
    assert settings.target_temp_f == 72.0
    assert settings.away_setback_f == 58.0
    assert settings.hvac_eta_min == 25
    assert settings.lights_backend == "hue"
    assert settings.hue_bridge_ip == "192.168.1.5"
    assert settings.lights_eta_min == 4
    assert settings.lights_auto_off_min == 10
    assert settings.tv_backend == "samsung"
    assert settings.tv_input == "HDMI 1"
    assert settings.console_wol is False
    assert settings.ai_mode == "never"
    assert settings.server_port == 8644
    assert [v.name for v in settings.vehicles] == ["Model Y"]


def test_wizard_minimal_skips_write_loadable_env(tmp_path):
    answers = [
        "cid", "sec", "Solo", "VIN1", "",
        "Casa", "1 Main St",
        "y",                                  # confirm the single match
        "1",                                  # thermostat: none
        "1",                                  # lights: none
        "1",                                  # TV: none
        *TAIL,
    ]
    env_file = tmp_path / ".env"
    run_setup(input_fn=_scripted(answers), out=lambda *a: None,
              env_path=str(env_file), geocode_fn=_fake_geocode(
                  [("1 Main St, Palmdale, CA, USA", 34.5, -118.1)]))
    settings = load_settings(_parse_env(env_file))
    assert settings.home_name == "Casa"
    assert settings.home_lat == 34.5
    assert settings.thermostat_backend == "none"
    assert settings.lights_backend == "none"
    assert settings.tv_backend == "none"


def test_wizard_geocode_failure_maps_link_fallback(tmp_path):
    from tesla_home_link.setup import GeocodeError

    def broken(address, timeout=12.0):
        raise GeocodeError("Couldn't reach the address lookup service")

    answers = [
        "cid", "sec", "Solo", "VIN1", "",
        "",                                   # home name default
        "123 Main St, Palmdale, CA",          # address
        "2",                                  # fallback: paste a Maps link
        "https://www.google.com/maps/place/Somewhere/@34.5794,-118.1185,15z",
        "1", "1", "1",                        # thermostat/lights/TV: none
        *TAIL,
    ]
    env_file = tmp_path / ".env"
    run_setup(input_fn=_scripted(answers), out=lambda *a: None,
              env_path=str(env_file), geocode_fn=broken)
    settings = load_settings(_parse_env(env_file))
    assert settings.home_lat == 34.5794
    assert settings.home_lon == -118.1185


def test_parse_maps_link():
    assert parse_maps_link(
        "https://www.google.com/maps/place/X/@34.5794,-118.1185,15z") == (34.5794, -118.1185)
    assert parse_maps_link("https://maps.google.com/?q=34.24,-118.10") == (34.24, -118.10)
    assert parse_maps_link("https://www.google.com/maps") is None


def test_wizard_keeps_existing_when_declined(tmp_path):
    env_file = tmp_path / ".env"
    env_file.write_text("THL_VEHICLES=Keep=VIN0\n")
    run_setup(input_fn=_scripted(["n"]), out=lambda *a: None,
              env_path=str(env_file))
    assert "Keep" in env_file.read_text()


def test_wizard_multiple_geocode_matches_numbered_choice(tmp_path):
    answers = [
        "cid", "sec", "Solo", "VIN1", "",
        "", "Main St",
        "2",                                  # pick the 2nd match
        "1", "1", "1",
        *TAIL,
    ]
    env_file = tmp_path / ".env"
    run_setup(input_fn=_scripted(answers), out=lambda *a: None,
              env_path=str(env_file),
              geocode_fn=_fake_geocode([
                  ("Main St, Lancaster, CA, USA", 34.6941, -118.1367),
                  ("Main St, Palmdale, CA, USA", 34.5794, -118.1185),
              ]))
    settings = load_settings(_parse_env(env_file))
    assert settings.home_lat == 34.5794
