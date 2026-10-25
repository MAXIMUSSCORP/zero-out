"""The `zeroout` command: setup, run, crawl."""
import argparse
import dataclasses
import os
import secrets
import sys
import threading
import webbrowser

from .config import load_settings
from . import home

HOST = "127.0.0.1"  # never 0.0.0.0: this app is for the person at the keyboard
BROWSER_DELAY = 1.0


def lock_down() -> None:
    """Keep other local users out of the settings and the data."""
    if os.name != "posix":
        return
    for d in {home.config_dir(), home.data_dir()}:
        if d.is_dir():
            d.chmod(0o700)


def run(args, uvicorn=None, browser=None) -> int:
    if not os.environ.get("ZEROOUT_ENV_FILE", "").strip() and not home.config_file().is_file():
        print("  Not set up yet. Run: zeroout setup")
        return 1
    if uvicorn is None:
        import uvicorn
    browser = browser or webbrowser.open
    from .web import LocalGuard, create_app

    # Session cookies must work over plain http on localhost.
    settings = dataclasses.replace(load_settings(), cookie_secure=False)
    guard = LocalGuard(port=args.port, token=secrets.token_urlsafe(32))
    try:
        app = create_app(settings, local=guard)
    except RuntimeError as e:
        print(f"  {e}")
        return 1
    url = f"http://{HOST}:{args.port}/start?t={guard.token}"
    lock_down()
    print(f"  Settings  {settings.config_path}")
    print(f"  Open      {url}")
    if not args.no_browser:
        timer = threading.Timer(BROWSER_DELAY, browser, args=(url,))
        timer.daemon = True
        timer.start()
    uvicorn.run(app, host=HOST, port=args.port, proxy_headers=False)
    return 0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="zeroout")
    sub = ap.add_subparsers(dest="command", required=True)
    sub.add_parser("setup", help="set up this computer", add_help=False)
    r = sub.add_parser("run", help="run the app on this computer")
    r.add_argument("--port", type=int, default=8000)
    r.add_argument("--no-browser", action="store_true", help="do not open the browser")
    sub.add_parser("crawl", help="map one company", add_help=False)
    args, rest = ap.parse_known_args(argv)
    if args.command == "setup":
        from . import setup
        return setup.main(rest) or 0
    if args.command == "crawl":
        from . import crawl
        return crawl.main(rest) or 0
    if rest:
        ap.error(f"unrecognized arguments: {' '.join(rest)}")
    return run(args)


if __name__ == "__main__":
    sys.exit(main())
