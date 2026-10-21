# Tesla Home Link — Your Tesla Warms Up the House Before You Get Home

**Free & open source** car↔home continuity for **Tesla Model 3, Model Y, Model S, Model X and Cybertruck** via the **Tesla Fleet API** — Xiaomi HyperOS-style: while you drive home, your house gets ready.

▶ **Try the live demo: https://rtmalikian.github.io/tesla-home-link/** (interactive, pretend data)

Tesla is an island — nothing talks to your house. Xiaomi's HyperOS cars join the home ecosystem: the car tells the thermostat you're 20 minutes out, the porch lights come on as you turn onto your street, the TV and console are awake before the door opens. This app gives Tesla owners exactly that, free and open source, with **no subscription and no cloud account** — everything runs on your own computer.

## Why?

- *"I want to walk into a warm house in winter without leaving the heat blasting all day while I'm at work."*
- *"My driveway is pitch black when I get home late — I want the porch lights on before I pull in, not after I fumble for the switch."*
- *"It'd be nice if the TV and PlayStation were already on when I walk in. First-world problem, but still."*
- *"I always forget to set the thermostat back when I leave for the weekend."*

No free tool did all of this for Tesla. Tessie and TezLab charge subscriptions and live in someone else's cloud. Tesla Home Link is a small Python app you run yourself: it watches your car's navigation (read-only — it never sends a command to the car), and talks to your **local** home devices instead.

## What it does

| Trigger | Action |
|---|---|
| You're navigating home (or closing in on it) | **Welcome scene**: TV on → right input → your app; game console woken from standby (Wake-on-LAN); smart-plug power restored for older consoles |
| ETA home ≤ 20 min (scales with how cold/hot the house is) | **Thermostat pre-arrival**: Nest, Ecobee, Home Assistant, or any webhook — the house is at your comfort temp when you walk in |
| ETA home ≤ 5 min **and** it's after civil dusk | **Porch/entry lights on**: Philips Hue (local bridge) or webhook; auto-off 15 min after arrival |
| You drive >1 km away from home | **Away mode**: thermostat setback, lights off, optional alarm webhook |
| Trip diverted (nav changes, or you park elsewhere 10+ min) | **Abort**: the HVAC setpoint is restored, and the rollback is logged |

Detection is two-layer: the primary signal is your car's **active navigation destination** (≈ home); the fallback is a **closing-distance detector** that needs ≥3 consecutive shrinking reads inside a 12 km geofence — a drive that merely passes your neighborhood with the nav set elsewhere never fires (pass-by suppression). Every trigger lands in an append-only decision log, so you can ask *"why did the heat start at 6:42pm?"* and get the real answer.

```
 5:34 PM  ★ welcome scene: TV on → HDMI 1 → YouTube, console awake, plug powered
 5:54 PM  ★ pre-heat to 71°F (ETA 22 min, it's 64°F inside — cold house, early start)
 6:59 PM  ★ porch lights on (ETA 4 min, after dusk)
 7:08 PM     arrived home
 7:24 PM  ★ porch lights auto-off (15 min after arrival)
```

## Quickstart (owners, not developers)

```bash
git clone https://github.com/rtmalikian/tesla-home-link.git
cd tesla-home-link
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
cp .env.example .env
.venv/bin/python -m tesla_home_link.cli setup     # guided Q&A, writes .env for you
.venv/bin/python -m tesla_home_link.cli auth       # one-time Tesla sign-in (read-only)
.venv/bin/python -m tesla_home_link.cli simulate   # watch a pretend evening drive
.venv/bin/python -m tesla_home_link.cli run --dry-run   # preview with your real setup
.venv/bin/python -m tesla_home_link.cli run        # first real run asks for confirmation
```

Or install the `thlink` command: `.venv/bin/pip install -e .`

`thlink setup` asks plain-language questions — your home by **name + street address** (geocoded automatically; or paste a Google Maps link), temperatures in **°F**, times in **minutes**, devices as **numbered lists**. It never asks you to type coordinates or edit config syntax.

Schedule it with cron (every 2 minutes during your commute windows is plenty):

```bash
*/2 16-20 * * * cd ~/tesla-home-link && .venv/bin/thlink run >> ~/.thlink/run.log 2>&1
```

## Tesla developer setup

1. Go to [developer.tesla.com](https://developer.tesla.com), sign in, **Create App**.
2. Copy the **Client ID** and **Client Secret** into `thlink setup` (or `.env`).
3. Scopes needed: `openid`, `offline_access`, `vehicle_device_data`, `vehicle_location`. **No command scopes** — this app never sends a command to your car (no virtual-key pairing needed).

## How it works

Every `thlink run` is one decision cycle: a no-wake read of each car (location + navigation destination + ETA), indoor temperature and porch-light state from your home backends, then the pure rules engine (`home.py`) decides. Actions execute against your **home devices over your local network / webhooks** — the car is strictly read-only.

**Expected API cost:** ≈ **$4/month for one car** at community-reported rates ($1 per 500 data calls) — ~64 no-wake reads/day duty-cycled (reads while commuting + hourly parked checks), **0 car commands, 0 wake-ups**. Raise `THL_POLL_SECONDS` or narrow the cron window to cut it further. Re-check live pricing at developer.tesla.com.

## AI endpoints

Every Series-2 app embeds an OpenAI-compatible surface (stdlib only, no extra deps):

```bash
# what can it do?
curl http://127.0.0.1:8644/v1/models

# explain a trigger from the decision log
curl http://127.0.0.1:8644/v1/chat/completions -H 'Content-Type: application/json' \
  -d '{"model":"tesla-home-link","messages":[{"role":"user",
       "content":"why did the heat start at 6:42pm?"}]}'

# change a rule in plain language (rules engine applies it deterministically)
curl http://127.0.0.1:8644/v1/chat/completions -H 'Content-Type: application/json' \
  -d '{"model":"tesla-home-link","messages":[{"role":"user",
       "content":"start the heat 30 min out"}]}'
```

The chat surface answers **rules-first**: *"why"* questions are answered from the append-only decision log, and natural-language rule edits (*"start the heat 30 min out"*, *"lights at 3 minutes"*, *"keep it at 72"*) are parsed into allowlisted `THL_*` settings and persisted to `.env`. An optional LLM backend (`THL_AI_*`, default local Ollama) handles free-form questions and may emit `set_home_rule` command blocks — validated against the same allowlist before anything changes. With `THL_AI_MODE=never` (or no backend) everything still works; the LLM never sees your data unless you configure it. Streaming is not supported (`stream=true` is ignored). Set `THL_SERVER_API_KEY` to require a bearer token.

## `.env` reference

| Variable | Default | What it does |
|---|---|---|
| `THL_HOME_NAME` / `THL_HOME_ADDRESS` / `THL_HOME_LAT` / `THL_HOME_LON` | — | Your home (name is matched against nav destinations; address is geocoded by setup) |
| `THL_THERMOSTAT_BACKEND` | `none` | `nest` · `ecobee` · `homeassistant` · `webhook` · `none` |
| `THL_TARGET_TEMP_F` / `THL_AWAY_SETBACK_F` | `71` / `60` | Comfort temp on approach; energy-saving temp when you leave |
| `THL_HVAC_ETA_MIN` | `20` | Base pre-climate trigger; +3 min per °F of indoor gap, capped at +30 |
| `THL_LIGHTS_BACKEND` | `none` | `hue` · `webhook` · `none` |
| `THL_LIGHTS_ETA_MIN` / `THL_LIGHTS_AUTO_OFF_MIN` | `5` / `15` | Lights on at N min out (after dusk only); off M min after arrival |
| `THL_TV_BACKEND` / `THL_TV_INPUT` / `THL_TV_APP` | `none` | `samsung` · `lg`; input to switch to; app to open |
| `THL_CONSOLE_WOL` / `THL_CONSOLE_MACS` | `0` | Wake-on-LAN for standby consoles (comma-separated MACs) |
| `THL_CONSOLE_PLUG` / `THL_PLUG_WEBHOOK` | `0` | Smart-plug power restore (POST `{"action":"on"}`) |
| `THL_ALARM_WEBHOOK` | — | POST `{"action":"arm"}` when you leave |
| `THL_AI_BASE_URL` / `THL_AI_API_KEY` / `THL_AI_MODEL` / `THL_AI_MODE` | Ollama / — / `llama3.1` / `auto` | Pluggable OpenAI-compatible backend; `never` disables it |
| `THL_SERVER_PORT` / `THL_SERVER_API_KEY` | `8644` | Chat server bind port; optional bearer token |
| `THL_POLL_SECONDS` | `120` | Decision-cycle cadence (when run on a schedule) |
| `THL_DRY_RUN` | `0` | `1` = preview actions without touching home devices |

## FAQ

**Does it work without any smart-home gear?**
Yes. With no backends configured it still tracks approaches, logs every trigger, and explains them via `thlink explain` / the chat endpoint — a "what would have happened" mode. Add devices later with `thlink setup`.

**Why do the lights only come on after dusk?**
No point lighting the porch at noon. Dusk is computed from the date and your home's coordinates (proper solar math, no dependencies, no network). You can see the exact dusk time it used in the decision log.

**Will it fire if I just drive past my street?**
No — that's the pass-by suppression. Navigation set anywhere but home, or no sustained closing across ≥3 reads, means no trigger. The simulator (`thlink simulate`) includes a pass-by leg to prove it.

**What happens if I change plans mid-drive?**
The abort logic watches for it: if your navigation changes away from home, or you keep moving away for 10+ minutes, any pre-arrival HVAC change is rolled back to the previous setpoint and logged.

**Does it wake my car or send it commands?**
Never. The car is read-only: location + navigation reads only, no wake-ups, no commands. All actions target your home devices.

**Where does my data go?**
Nowhere you don't choose. Rules run locally; vehicle + home data are sent only to the LLM backend you configure (`THL_AI_BASE_URL`, default local Ollama). With `THL_AI_MODE=never`, nothing leaves the machine.

**Which thermostats / lights / TVs?**
Nest (SDM API), Ecobee, Home Assistant climate entities, or any generic webhook for thermostats; Philips Hue (local bridge API) or webhook for lights; Samsung / LG TVs plus Wake-on-LAN for consoles. Each device is individually toggleable.

## Development

```bash
.venv/bin/python -m pytest tests/ -q          # 65 tests
.venv/bin/python -m tesla_home_link.cli simulate
```

## License

MIT — Raphael Malikian. See [LICENSE](LICENSE).
