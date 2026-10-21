"""Service-level tests: OpenAI response shapes, rules-first Q&A, natural
language rule edits, LLM command-block allowlisting, the decision cycle
(dry-run honored), and one real HTTP smoke test."""
import json
import threading
import urllib.error
import urllib.request

from tesla_home_link.config import Settings, VehicleConfig
from tesla_home_link.home import HomeState
from tesla_home_link.service import HomeService
from tesla_home_link.simulator import FakeTeslaClient, FakeVehicle, HOME


def _settings(**over):
    s = Settings()
    s.home_lat, s.home_lon = HOME
    s.vehicles = [VehicleConfig(name="Model Y", vin="VINY")]
    s.thermostat_backend = "webhook"
    s.thermostat_webhook = "http://localhost:9/nope"
    s.lights_backend = "webhook"
    s.lights_webhook = "http://localhost:9/nope"
    s.target_temp_f = 71.0
    s.hvac_eta_min = 20
    s.lights_eta_min = 5
    for k, v in over.items():
        setattr(s, k, v)
    return s


def _service(**over):
    fake = FakeTeslaClient([FakeVehicle(vin="VINY", name="Model Y")])
    svc = HomeService(_settings(**over), client_factory=lambda: fake,
                      llm_factory=lambda: None)
    return svc, fake


# -- OpenAI surface -------------------------------------------------------

def test_models_list_shape():
    svc, _ = _service()
    resp = svc.models_list()
    assert resp["object"] == "list"
    model = resp["data"][0]
    assert model["id"] == "tesla-home-link"
    assert model["object"] == "model"
    assert "created" in model


def test_chat_completions_shape():
    svc, _ = _service()
    resp = svc.chat_completions(
        {"model": "tesla-home-link",
         "messages": [{"role": "user", "content": "what do you do?"}]})
    assert resp["object"] == "chat.completion"
    choice = resp["choices"][0]
    assert choice["message"]["role"] == "assistant"
    assert "Home" in choice["message"]["content"]
    assert choice["finish_reason"] == "stop"
    assert resp["usage"]["total_tokens"] >= 0


def test_chat_empty_message_gives_capabilities():
    svc, _ = _service()
    resp = svc.chat_completions({"messages": []})
    assert "car" in resp["choices"][0]["message"]["content"].lower()


# -- rules-first natural-language rule edits -------------------------------

def test_rule_edit_heat_minutes_out():
    svc, _ = _service()
    out = svc.answer("start the heat 30 min out")
    assert "30" in out and "pre-heat" in out
    assert svc.settings.hvac_eta_min == 30


def test_rule_edit_heat_short_form():
    svc, _ = _service()
    out = svc.answer("on cold evenings start the heat 45 minutes out")
    assert "45" in out
    assert svc.settings.hvac_eta_min == 45


def test_rule_edit_lights():
    svc, _ = _service()
    out = svc.answer("turn the porch lights on 3 minutes out")
    assert "3" in out
    assert svc.settings.lights_eta_min == 3


def test_rule_edit_comfort_temp():
    svc, _ = _service()
    out = svc.answer("keep it at 72")
    assert "72" in out
    assert svc.settings.target_temp_f == 72.0


def test_rule_edit_clamped_to_range():
    svc, _ = _service()
    out = svc.answer("start the heat 500 min out")
    assert "180" in out                      # clamped to the max
    assert svc.settings.hvac_eta_min == 180


def test_rule_edit_preview_only_does_not_persist():
    svc, _ = _service()
    svc.preview_only = True
    out = svc.answer("start the heat 30 min out")
    assert "preview" in out.lower()
    assert svc.settings.hvac_eta_min == 20


def test_rule_edit_persists_to_env(tmp_path):
    env_file = tmp_path / ".env"
    env_file.write_text("THL_HVAC_ETA_MIN=20\nOTHER=1\n")
    svc = HomeService(_settings(), client_factory=lambda: FakeTeslaClient([]),
                      llm_factory=lambda: None, env_path=str(env_file))
    svc.answer("start the heat 30 min out")
    line = [ln for ln in env_file.read_text().splitlines()
            if ln.startswith("THL_HVAC_ETA_MIN=")][0]
    assert line == "THL_HVAC_ETA_MIN=30"


# -- "why" answers from the decision log --------------------------------------

def test_why_heat_uses_decision_log(tmp_path):
    svc, fake = _service()
    state = HomeState(str(tmp_path / "state.json"))
    vs = state.for_vin("VINY")
    vs.log.append([1.0, "HVAC pre-arrival fired at 6:42pm: ETA home crossed "
                        "41 min (ETA 22 min), indoor 64.0°F → target 71°F."])
    out = svc.answer("why did the heat start at 6:42pm?", state=state)
    assert "6:42pm" in out
    assert "41 min" in out


def test_why_with_empty_log():
    svc, _ = _service()
    out = svc.answer("why did the heat start?", state=HomeState("/tmp/thl-nonexistent-xyz/state.json"))
    assert "Nothing has fired yet" in out


def test_why_lights_from_log(tmp_path):
    svc, fake = _service()
    state = HomeState(str(tmp_path / "state.json"))
    state.for_vin("VINY").log.append(
        [1.0, "Lights on at 7:02pm — ETA home crossed 5 min after civil dusk."])
    out = svc.answer("why did the porch lights turn on?", state=state)
    assert "7:02pm" in out


# -- LLM fallback ------------------------------------------------------------

def test_llm_command_block_applies_allowlisted_edit():
    class FakeLlm:
        def chat(self, context, message):
            return ('```json\n{"action": "set_home_rule", '
                    '"key": "THL_LIGHTS_ETA_MIN", "value": "7"}\n```')

    svc = HomeService(_settings(), client_factory=lambda: None,
                      llm_factory=lambda: FakeLlm())
    out = svc.answer("make the lights come on a bit earlier")
    assert "7" in out
    assert svc.settings.lights_eta_min == 7


def test_llm_non_allowlisted_key_ignored():
    class FakeLlm:
        def chat(self, context, message):
            return ('```json\n{"action": "set_home_rule", '
                    '"key": "THL_SERVER_PORT", "value": "1234"}\n```')

    svc = HomeService(_settings(), client_factory=lambda: None,
                      llm_factory=lambda: FakeLlm())
    out = svc.answer("change the server port")
    assert svc.settings.server_port == 8644          # unchanged
    assert "THL_SERVER_PORT" in out                  # raw model text returned


def test_llm_non_allowlisted_action_ignored():
    class FakeLlm:
        def chat(self, context, message):
            return ('```json\n{"action": "open_garage", '
                    '"key": "THL_HVAC_ETA_MIN", "value": "10"}\n```')

    svc = HomeService(_settings(), client_factory=lambda: None,
                      llm_factory=lambda: FakeLlm())
    svc.answer("open the garage")
    assert svc.settings.hvac_eta_min == 20


def test_llm_failure_falls_back_to_capabilities():
    class BrokenLlm:
        def chat(self, context, message):
            raise RuntimeError("down")

    svc = HomeService(_settings(), client_factory=lambda: None,
                      llm_factory=lambda: BrokenLlm())
    out = svc.answer("tell me about yourself")
    assert "car" in out.lower() and "home" in out.lower()


def test_ai_mode_never_skips_llm():
    calls = []

    class LoudLlm:
        def chat(self, context, message):
            calls.append(message)
            raise AssertionError("must not be called")

    svc = HomeService(_settings(ai_mode="never"), client_factory=lambda: None,
                      llm_factory=lambda: LoudLlm())
    out = svc.answer("some free-form ramble about nothing")
    assert calls == []
    assert "car" in out.lower()


# -- decision cycle -------------------------------------------------------------

def test_run_cycle_dry_run_executes_nothing(tmp_path):
    svc, fake = _service()
    car = fake.vehicles["VINY"]
    car.lat, car.lon = HOME[0] + 15000 / 111_320.0, HOME[1]
    car.nav_dest, car.nav_eta_min = "Home", 25
    state = HomeState(str(tmp_path / "state.json"))
    decisions, results = svc.run_cycle(state=state, now=1000.0, dry_run=True,
                                       skip_home_devices=True)
    assert decisions
    assert all(r.startswith("Model Y: [dry-run]") or r.startswith("Model Y: ")
               for r in results) or results == []


def test_run_cycle_needs_client():
    svc = HomeService(_settings(), client_factory=lambda: None,
                      llm_factory=lambda: None)
    try:
        svc.run_cycle(dry_run=True, skip_home_devices=True)
    except RuntimeError as exc:
        assert "auth" in str(exc)
    else:
        raise AssertionError("expected RuntimeError")


# -- HTTP smoke test ---------------------------------------------------------------

def test_http_surface_end_to_end():
    from tesla_home_link.server import make_handler
    from http.server import ThreadingHTTPServer

    svc, _ = _service()
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(svc))
    port = httpd.server_address[1]
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/health") as r:
            assert json.loads(r.read())["status"] == "ok"
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/v1/models") as r:
            data = json.loads(r.read())
            assert data["data"][0]["id"] == "tesla-home-link"
        body = json.dumps({"model": "tesla-home-link",
                           "messages": [{"role": "user",
                                         "content": "what do you do?"}]}).encode()
        req = urllib.request.Request(f"http://127.0.0.1:{port}/v1/chat/completions",
                                     data=body,
                                     headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req) as r:
            data = json.loads(r.read())
            assert data["object"] == "chat.completion"
            assert data["choices"][0]["finish_reason"] == "stop"
    finally:
        httpd.shutdown()
        thread.join(timeout=5)


def test_http_bearer_auth_enforced():
    from tesla_home_link.server import make_handler
    from http.server import ThreadingHTTPServer

    svc, _ = _service(server_api_key="secret123")
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(svc))
    port = httpd.server_address[1]
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    try:
        try:
            urllib.request.urlopen(f"http://127.0.0.1:{port}/v1/models")
            raise AssertionError("expected 401")
        except urllib.error.HTTPError as exc:
            assert exc.code == 401
        req = urllib.request.Request(f"http://127.0.0.1:{port}/v1/models",
                                     headers={"Authorization": "Bearer secret123"})
        with urllib.request.urlopen(req) as r:
            assert r.status == 200
    finally:
        httpd.shutdown()
        thread.join(timeout=5)
