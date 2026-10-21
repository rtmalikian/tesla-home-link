"""Transport-independent home-link service: takes OpenAI-shaped request
dicts, returns OpenAI-shaped response dicts. The HTTP server (server.py) is
a thin wrapper; tests drive this class directly.

Chat flow for a user message:
  1. Rules engine answers "why" questions from the decision log
     ("why did the heat start at 6:42pm?" -> the logged trigger line).
  2. Natural-language rule edits ("start the heat 30 min out", "keep it at
     72") are parsed rules-first into allowlisted THL_* keys, then applied
     to the live settings and persisted to .env. The optional LLM backend
     may emit an allowlisted {action: set_home_rule, ...} JSON command block
     as a second path; it is validated the same way.
  3. ai_mode=never (or LLM failure) -> capability-list answer.

run_cycle() performs one decision pass: no-wake fleet reads, home-device
context, home.decide() per car, then executes each action against the
configured backends (dry-run honored). It never issues a Fleet API car
command — the car is read-only; all actions are local home devices.
"""
from __future__ import annotations

import itertools
import re
import time
from pathlib import Path

from .config import ConfigError
from .devices import DeviceError, execute_action, lights_currently_on, thermostat_read_indoor_f
from .fleet import FleetSnapshot, snapshot_fleet
from .home import HomeDecision, HomeReading, HomeState, decide
from .llm import ALLOWED_RULE_KEYS, LlmBackend, extract_rule_command

CAPABILITY_BLURB = (
    "I connect your Tesla to your house — Xiaomi HyperOS-style car↔home "
    "continuity, free and open source. While you drive home I can start the "
    "heat or AC early, turn the porch lights on after dark, wake your TV "
    "and game console before you walk in, and set the house to away mode "
    "when you leave.\n"
    "I can explain any trigger from my decision log — ask \"why did the "
    "heat start at 6:42pm?\" — and I can change the rules in plain language: "
    "\"start the heat 30 min out\", \"lights at 3 minutes\", \"keep it at "
    "72\". Connect an LLM backend (THL_AI_BASE_URL) and I can handle "
    "free-form questions too. `thlink run` applies the rules; with no "
    "backend configured I still track and explain everything."
)

# Rules-first NL patterns (checked before the LLM). Order: edits first.
_HEAT_WORDS = r"(?:heat|hvac|heating|cooling|a/?c|climate|warm(?:ing)?\s+up)"
_RULE_EDIT_PATTERNS = [
    # "start the heat 30 min out", "turn on the AC 25 minutes early"
    (re.compile(rf"(?:start|turn\s+on|kick\s+off)\s+(?:the\s+)?{_HEAT_WORDS}"
                rf"\s+(?:at\s+|after\s+)?(\d{{1,3}})\s*(?:min|minutes?)"
                rf"(?:\s*(?:out|early|before))?\b"), "THL_HVAC_ETA_MIN"),
    # "heat 30 min out"
    (re.compile(rf"{_HEAT_WORDS}\s+(\d{{1,3}})\s*(?:min|minutes?)"
                rf"\s*(?:out|early|before)\b"), "THL_HVAC_ETA_MIN"),
    # "lights at 3 minutes", "turn the porch lights on 5 min out"
    (re.compile(rf"(?:lights?|porch).{{0,30}}?(\d{{1,3}})\s*(?:min|minutes?)"
                rf"(?:\s*(?:out|early|before))?\b"), "THL_LIGHTS_ETA_MIN"),
    # "keep it at 72", "set the house to 71", "make home 70 degrees"
    (re.compile(r"(?:keep|set|make)\s+(?:it|the\s+house|home|the\s+temp(?:erature)?)"
                rf"\s+(?:at|to)\s+(\d{{2,3}})\s*(?:°|degrees?|f\b)?"), "THL_TARGET_TEMP_F"),
    # "setback to 58", "away temp at 60"
    (re.compile(r"(?:setback|away.{0,12}temp(?:erature)?).{0,20}?(\d{2,3})"
                r"\s*(?:°|degrees?|f\b)?"), "THL_AWAY_SETBACK_F"),
]

_WHY_PATTERNS = {
    "why": re.compile(r"\bwhy\b"),
    "list": re.compile(r"\b(what do you do|capabilit|help|status|how does this work)\b"),
}

# Sane ranges per key (min, max, unit for the confirmation message).
_RULE_RANGES = {
    "THL_HVAC_ETA_MIN": (1, 180, "minutes before arrival"),
    "THL_LIGHTS_ETA_MIN": (1, 60, "minutes before arrival"),
    "THL_TARGET_TEMP_F": (50, 90, "°F"),
    "THL_AWAY_SETBACK_F": (45, 75, "°F"),
    "THL_LIGHTS_AUTO_OFF_MIN": (0, 120, "minutes"),
    "THL_APPROACH_GEOFENCE_M": (500, 100000, "metres"),
}

_RULE_LABELS = {
    "THL_HVAC_ETA_MIN": "pre-heat/cool trigger",
    "THL_LIGHTS_ETA_MIN": "porch-lights trigger",
    "THL_TARGET_TEMP_F": "comfort temperature",
    "THL_AWAY_SETBACK_F": "away setback temperature",
    "THL_LIGHTS_AUTO_OFF_MIN": "lights auto-off delay",
    "THL_APPROACH_GEOFENCE_M": "approach detection range",
}


class HomeService:
    def __init__(self, settings, client_factory=None, llm_factory=None,
                 env_path: str | None = None, preview_only: bool = False):
        self.settings = settings
        self.client_factory = client_factory or (lambda: None)
        self.llm_factory = llm_factory
        self.env_path = Path(env_path) if env_path else None
        self.preview_only = preview_only
        self._ids = itertools.count(1)

    # -- plumbing ------------------------------------------------------
    def _client(self):
        return self.client_factory()

    def _llm(self):
        if self.settings.ai_mode == "never":
            return None
        if self.llm_factory is not None:
            return self.llm_factory()
        if not self.settings.ai_base_url:
            return None
        return LlmBackend(self.settings.ai_base_url, self.settings.ai_api_key,
                          self.settings.ai_model, self.settings.ai_timeout_seconds)

    def _next_id(self, prefix: str) -> str:
        return f"{prefix}-{int(time.time())}-{next(self._ids)}"

    def _state(self, state: "HomeState | None") -> HomeState:
        return state or HomeState(self.settings.state_file)

    # -- OpenAI surface ------------------------------------------------
    def models_list(self) -> dict:
        return {"object": "list", "data": [{
            "id": self.settings.model_id,
            "object": "model",
            "created": int(time.time()),
            "owned_by": "tesla-home-link",
        }]}

    def chat_completions(self, body: dict) -> dict:
        messages = body.get("messages") or []
        user_text = ""
        for m in reversed(messages):
            if m.get("role") == "user":
                user_text = str(m.get("content") or "")
                break
        text = self.answer(user_text)
        return {
            "id": self._next_id("chatcmpl"),
            "object": "chat.completion",
            "created": int(time.time()),
            "model": self.settings.model_id,
            "choices": [{
                "index": 0,
                "message": {"role": "assistant", "content": text},
                "finish_reason": "stop",
            }],
            "usage": {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0},
        }

    # -- rules-first chat -----------------------------------------------
    def home_brief(self) -> str:
        s = self.settings
        lines = [
            f"Home: {s.home_name}" + (f" ({s.home_address})" if s.home_address else ""),
            f"Thermostat: {s.thermostat_backend} — comfort {s.target_temp_f:g}°F, "
            f"away {s.away_setback_f:g}°F, pre-climate at "
            f"{s.hvac_eta_min} min out (scales with the temperature gap)",
            f"Lights: {s.lights_backend} — on at {s.lights_eta_min} min out "
            f"(after dusk only), off {s.lights_auto_off_min} min after arrival",
            f"Entertainment: TV {s.tv_backend}"
            + (f" → {s.tv_input}" if s.tv_input else "")
            + (f" → {s.tv_app}" if s.tv_app else "")
            + (", console Wake-on-LAN" if s.console_wol else "")
            + (", console smart-plug" if s.console_plug else ""),
        ]
        return "\n".join(lines)

    def answer(self, user_text: str, state: "HomeState | None" = None) -> str:
        """Answer a chat message using rules first, LLM as fallback."""
        text = user_text.strip()
        if not text:
            return CAPABILITY_BLURB
        lowered = text.lower()

        # 1) natural-language rule edits
        for pat, key in _RULE_EDIT_PATTERNS:
            m = pat.search(lowered)
            if m:
                return self._apply_rule_edit(key, m.group(1))

        # 2) "why" questions -> decision log
        if _WHY_PATTERNS["why"].search(lowered):
            return self._answer_why(text, state)

        # 3) capability / status questions
        if _WHY_PATTERNS["list"].search(lowered):
            return ("Here's how I'm set up:\n" + self.home_brief() + "\n\n"
                    "Run `thlink run` (dry-run first) to apply the rules, or "
                    "`thlink simulate` to watch a pretend evening drive.")

        # 4) LLM fallback
        return self._llm_answer(user_text)

    # -- rule edits -------------------------------------------------------
    def _apply_rule_edit(self, key: str, value_raw: str) -> str:
        if key not in ALLOWED_RULE_KEYS:
            return (f"I can only change these settings: "
                    f"{', '.join(sorted(ALLOWED_RULE_KEYS))}.")
        kind = ALLOWED_RULE_KEYS[key]
        try:
            value = int(value_raw) if kind == "int" else float(value_raw)
        except (TypeError, ValueError):
            return f"\"{value_raw}\" isn't a number — say it as one, like \"30\"."
        lo, hi, unit = _RULE_RANGES[key]
        clamped = max(lo, min(hi, value))
        old = getattr(self.settings, _env_to_attr(key))
        label = _RULE_LABELS[key]
        if self.preview_only:
            note = " (preview only — not saved; re-run without preview to apply)"
            if clamped != value:
                return (f"I'd change the {label} to {clamped:g} {unit} "
                        f"(out of range — {value:g} was clamped). Currently "
                        f"{old:g}.{note}")
            return (f"I'd change the {label} from {old:g} to {clamped:g} "
                    f"{unit}.{note}")
        setattr(self.settings, _env_to_attr(key), clamped)
        self._persist_rule(key, clamped)
        if clamped != value:
            return (f"Changed the {label} to {clamped:g} {unit} ({value:g} was "
                    f"out of range). Was {old:g}.")
        return f"Changed the {label} from {old:g} to {clamped:g} {unit}."

    def _persist_rule(self, key: str, value) -> None:
        """Write one THL_* setting back to the .env file."""
        if self.env_path is None or not self.env_path.exists():
            return
        lines = []
        seen = False
        for line in self.env_path.read_text().splitlines():
            if line.startswith(f"{key}="):
                lines.append(f"{key}={value:g}" if isinstance(value, float)
                             else f"{key}={value}")
                seen = True
            else:
                lines.append(line)
        if not seen:
            lines.append(f"{key}={value:g}" if isinstance(value, float)
                         else f"{key}={value}")
        self.env_path.write_text("\n".join(lines) + "\n")

    # -- "why" answers from the decision log --------------------------------
    def _answer_why(self, user_text: str, state: "HomeState | None") -> str:
        st = self._state(state)
        entries: list[tuple[float, str]] = []
        for vs in st.vehicles.values():
            entries.extend((ts, text) for ts, text in vs.log)
        if not entries:
            return ("Nothing has fired yet — no triggers in the decision log. "
                    "Run `thlink simulate` to watch what a drive home looks "
                    "like, or `thlink run` to apply the rules for real.")
        lowered = user_text.lower()
        heat_words = ("heat", "hvac", "warm", "cool", "a/c", "climate", "thermostat")
        light_words = ("light", "porch", "lamp")
        if any(w in lowered for w in heat_words):
            heat = [(ts, t) for ts, t in entries if "HVAC" in t]
            if heat:
                ts, t = heat[-1]
                return self._explain_entry(t, what="the heat")
            return ("The HVAC pre-arrival hasn't fired yet — the decision log "
                    "has no heat trigger.")
        if any(w in lowered for w in light_words):
            lights = [(ts, t) for ts, t in entries if "Light" in t or "light" in t]
            if lights:
                ts, t = lights[-1]
                return self._explain_entry(t, what="the porch lights")
            return "The porch lights haven't been triggered yet."
        ts, t = entries[-1]
        return (f"Most recent trigger:\n{t}\n\n(Ask \"why did the heat start "
                "at 6:42pm?\" for a specific event.)")

    def _explain_entry(self, log_line: str, what: str) -> str:
        return f"Here's why {what} started:\n{log_line}"

    # -- LLM fallback -------------------------------------------------------
    def _llm_answer(self, user_text: str) -> str:
        llm = self._llm()
        if llm is None:
            return CAPABILITY_BLURB
        try:
            reply = llm.chat(self.home_brief(), user_text)
        except Exception:
            return CAPABILITY_BLURB + " (The AI backend didn't respond just now.)"
        cmd = extract_rule_command(reply)
        if cmd is None:
            return reply
        _action, key, value = cmd
        return self._apply_rule_edit(key, value)

    # -- decision cycle -----------------------------------------------------
    def run_cycle(self, state: "HomeState | None" = None,
                  now: float | None = None,
                  dry_run: bool | None = None,
                  skip_home_devices: bool = False) -> tuple[list[HomeDecision], list[str]]:
        """One decision pass: no-wake fleet reads -> home.decide() per car ->
        execute each home-device action (unless dry-run). Returns
        (decisions, human-readable action results)."""
        client = self._client()
        if client is None:
            raise RuntimeError("No Tesla client — run `thlink auth` first.")
        dry_run = self.settings.dry_run if dry_run is None else dry_run
        now = time.time() if now is None else now
        state = self._state(state)
        snap: FleetSnapshot = snapshot_fleet(client, self.settings.vehicles)

        indoor = None if skip_home_devices else thermostat_read_indoor_f(self.settings)
        porch_on = None if skip_home_devices else lights_currently_on(self.settings)

        decisions: list[HomeDecision] = []
        results: list[str] = []
        for reading in snap.readings:
            reading.indoor_temp_f = indoor
            reading.lights_on = porch_on
            decision = decide(reading, state, self.settings, now=now)
            decisions.append(decision)
            for action in decision.actions:
                try:
                    outcome = execute_action(action, self.settings, dry_run=dry_run)
                except DeviceError as exc:
                    outcome = f"couldn't {action.label}: {exc}"
                results.append(f"{reading.name}: {outcome}")
        state.save()
        return decisions, results


def _env_to_attr(key: str) -> str:
    """THL_HVAC_ETA_MIN -> hvac_eta_min."""
    return key[len("THL_"):].lower()
