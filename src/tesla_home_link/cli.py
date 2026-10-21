"""CLI: thlink setup | auth | serve | explain | run | simulate | status"""
from __future__ import annotations

import argparse
import logging
import os
import sys

from .config import ConfigError, load_settings

__version__ = "0.1.0"


def _setup_logging(verbose: bool) -> None:
    logging.basicConfig(level=logging.DEBUG if verbose else logging.INFO,
                        format="%(levelname)s %(name)s: %(message)s")


def _client(settings):
    from .tesla_client import TeslaClient
    return TeslaClient(settings.tesla_client_id, settings.tesla_client_secret,
                       region=settings.tesla_region,
                       redirect_uri=settings.tesla_redirect_uri,
                       token_file=settings.token_file)


def _env_path(args) -> str:
    if getattr(args, "env_file", None):
        return args.env_file
    if os.path.exists(".env"):
        return ".env"
    return ".env"


def cmd_setup(args, settings) -> int:
    from .setup import run_setup
    run_setup(env_path=_env_path(args))
    return 0


def cmd_auth(args, settings) -> int:
    from .tesla_client import TeslaClient
    client = _client(settings)
    print("This app only READS your car's location and navigation — it never")
    print("sends commands to the car.")
    print()
    print("Open this URL in your browser and authorize:")
    print(client.authorize_url())
    print()
    code = input("Paste the 'code' from the redirected URL here > ").strip()
    if not code:
        print("No code pasted — nothing was saved. Run `thlink auth` again "
              "when you're ready.")
        return 1
    try:
        client.exchange_code(code)
    except Exception as exc:
        print(f"That didn't work: {exc}", file=sys.stderr)
        print("Double-check the code was copied in full, then run `thlink auth` again.",
              file=sys.stderr)
        return 1
    print("Signed in — your tokens are stored on this computer only.")
    return 0


def cmd_serve(args, settings) -> int:
    from .server import serve
    from .service import HomeService

    def client_factory():
        return _client(settings)

    service = HomeService(settings, client_factory=client_factory,
                          env_path=_env_path(args))
    print("Vehicle + home data go ONLY to your configured LLM backend "
          f"(THL_AI_BASE_URL={settings.ai_base_url or 'none'}); "
          "the rules engine works with no backend at all.")
    serve(service)
    return 0


def cmd_explain(args, settings) -> int:
    """One-shot Q&A — always a dry run: answers only, nothing is saved or sent."""
    from .home import HomeState
    from .service import HomeService

    service = HomeService(settings, client_factory=lambda: None,
                          llm_factory=lambda: None, preview_only=True)
    state = HomeState(settings.state_file)
    print(service.answer(args.question, state=state))
    print()
    print("(Dry run: nothing was saved and no device was touched.)")
    return 0


def cmd_run(args, settings) -> int:
    """One decision cycle: preview first, confirm before the first real send."""
    from .home import HomeState
    from .service import HomeService

    if not settings.vehicles:
        print("THL_VEHICLES is not set — run `thlink setup` first.", file=sys.stderr)
        return 2
    if not settings.home_lat or not settings.home_lon:
        print("Your home location isn't set — run `thlink setup` first.",
              file=sys.stderr)
        return 2

    dry_run = args.dry_run or settings.dry_run
    client = _client(settings)
    service = HomeService(settings, client_factory=lambda: client)
    state = HomeState(settings.state_file)

    print("Checking your cars against your home rules…")
    try:
        decisions, results = service.run_cycle(dry_run=dry_run)
    except Exception as exc:
        print(f"Couldn't check your cars just now: {exc}", file=sys.stderr)
        print("If this mentions tokens, run `thlink auth` first.", file=sys.stderr)
        return 1

    for d in decisions:
        print(f"  • {d.reason}")
    if not results:
        print("Nothing to trigger right now — no car is approaching home.")
        return 0

    print()
    print("Home-device actions:")
    for r in results:
        print(f"  {r}")
    if dry_run:
        print()
        print("Dry run — no home device was touched.")
        return 0

    if not state.meta.get("first_run_confirmed") and not args.yes:
        answer = input("Run these actions on your real home devices? (y/n) > ").strip().lower()
        if answer not in ("y", "yes"):
            print("OK — nothing was run. Re-run any time.")
            return 0
        state.meta["first_run_confirmed"] = True
        state.save()
        print("Confirmed — you won't be asked again for future runs.")
    print("Done.")
    return 0


def cmd_status(args, settings) -> int:
    from .fleet import snapshot_fleet
    client = _client(settings)
    snap = snapshot_fleet(client, settings.vehicles)
    for r in snap.readings:
        state = "asleep" if r.asleep else "awake"
        nav = f"navigating to {r.nav_dest} ({r.nav_eta_min:.0f} min out)" \
            if r.nav_dest else "not navigating"
        print(f"{r.name}: {state}, {nav}")
    for vin, err in snap.unreachable.items():
        print(f"{vin}: couldn't reach it — {err}")
    return 0


def cmd_simulate(args, settings) -> int:
    from .simulator import run_demo
    for line in run_demo():
        print(line)
    return 0


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="thlink",
                                description="Car-to-home continuity for your Teslas: "
                                            "pre-heat, porch lights, and a welcome-home "
                                            "scene that fire as you drive home.")
    p.add_argument("--version", action="version", version=f"thlink {__version__}")
    p.add_argument("--env-file", default=None)
    p.add_argument("-v", "--verbose", action="store_true")
    sub = p.add_subparsers(dest="command", required=True)
    sub.add_parser("setup", help="Guided first-run setup (plain-language questions)")
    sub.add_parser("auth", help="Authorize with your Tesla account (one-time, read-only)")
    sub.add_parser("serve", help="Run the OpenAI-compatible API server")
    exp_p = sub.add_parser("explain", help="Ask why something fired (dry run — nothing saved)")
    exp_p.add_argument("question", help="e.g. \"why did the heat start at 6:42pm?\"")
    run_p = sub.add_parser("run", help="One decision cycle over the fleet")
    run_p.add_argument("--dry-run", action="store_true",
                       help="Preview only — report actions instead of running them")
    run_p.add_argument("--yes", "-y", action="store_true",
                       help="Skip the first-run confirmation prompt")
    sub.add_parser("status", help="Show each car's navigation state")
    sub.add_parser("simulate", help="Demo the rules against a pretend evening drive")
    return p


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    _setup_logging(args.verbose)
    env = dict(os.environ)
    if args.env_file:
        from dotenv import load_dotenv
        load_dotenv(args.env_file, override=True)
        env = dict(os.environ)
    else:
        from dotenv import load_dotenv
        load_dotenv(override=False)
        env = dict(os.environ)
    try:
        settings = load_settings(env)
    except ConfigError as exc:
        print(f"Config error: {exc}", file=sys.stderr)
        return 2
    handlers = {"setup": cmd_setup, "auth": cmd_auth, "serve": cmd_serve,
                "explain": cmd_explain, "run": cmd_run, "status": cmd_status,
                "simulate": cmd_simulate}
    try:
        return handlers[args.command](args, settings)
    except SystemExit as exc:
        return int(exc.code or 0)
    except BrokenPipeError:
        return 0
    except KeyboardInterrupt:
        print("\nCancelled.")
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
