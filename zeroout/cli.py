"""The `zeroout` command: setup, run, crawl."""
import argparse
import dataclasses
import os
import secrets
import sys
import threading
import webbrowser
from datetime import datetime, timezone

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
    try:
        settings = dataclasses.replace(load_settings(), cookie_secure=False)
    except ValueError as e:
        print(f"  {e}")
        return 1
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


def _settings():
    try:
        return load_settings()
    except ValueError as e:
        print(f"  {e}")
        return None


def contribute(args) -> int:
    settings = _settings()
    if settings is None:
        return 1
    from . import community, db, github
    conn = db.connect(settings.db_path)
    row = conn.execute("SELECT name, number FROM companies WHERE id = ?", (args.company_id,)).fetchone()
    if row is None:
        print(f"  No company with id {args.company_id}.")
        return 1
    mine = community.build_contribution(conn, args.company_id)
    if mine is None:
        print(f"  Nothing new to share for {row['name']}.")
        return 0
    n = len(mine.observation_ids)
    sources = ", ".join(f"{k} {v}" for k, v in sorted(github._sources(mine.doc).items()))
    print(f"  Company   {row['name']} ({row['number']})")
    print(f"  Ready     {n} observation{'' if n == 1 else 's'} from this computer")
    if sources:
        print(f"  Sources   {sources}")
    print(f"  File      {mine.relpath}")
    login = github.github_login()
    if login:
        print(f"  Account   @{login}")
        print(f"  Public    This opens a public pull request on {settings.community_repo} as @{login}. "
              "The file is public as soon as you share it, before anyone reviews it, "
              "and GitHub keeps it even if the pull request is closed.")
    else:
        print("  Account   none. GitHub CLI is not set up, so the file would be saved to your outbox folder "
              f"and not posted. If you add it to {settings.community_repo} yourself, it is public as soon as you do.")
    print(mine.body.decode("utf-8", "replace").rstrip("\n"))
    print("  Check it for names before you share.")
    if not args.yes:
        try:
            answer = input("  Share? [y/N] ")
        except EOFError:
            answer = ""
        if answer.strip().lower() not in ("y", "yes"):
            print("  Not shared.")
            return 0
    try:
        sent = github.submit(mine, settings.community_repo, outbox_dir=home.data_dir() / "outbox")
    except Exception:
        print("  Something went wrong sharing that file. Nothing was marked as shared.")
        return 1
    if sent.url or sent.outbox or sent.partial:
        community.mark_contributed(conn, mine.observation_ids, datetime.now(timezone.utc).isoformat())
    if sent.partial:
        print(f"  Partial   {sent.message}")
        return 1
    if sent.url:
        print(f"  Shared    {sent.url}")
    else:
        print(f"  Saved     {sent.outbox}")
        print(f"  {sent.message}")
    return 0


def update(args) -> int:
    settings = _settings()
    if settings is None:
        return 1
    from . import community, db, github
    from .tree import nano_same_meaning
    conn = db.connect(settings.db_path)
    try:
        download = github.download_trees(settings.community_repo)
        r = community.import_files(conn, download.files, same_meaning=nano_same_meaning(settings),
                                   truncated=download.truncated)
    except github.CommunityError as e:
        print(f"  {e}")
        return 1
    except Exception:
        print("  Something went wrong updating from the community.")
        return 1
    bad = list(r.invalid)
    print(f"  Checked   {r.files} file{'' if r.files == 1 else 's'}")
    print(f"  Added     {r.observations_added} observation{'' if r.observations_added == 1 else 's'}")
    print(f"  Had       {r.already_had} already")
    if r.offered:
        print(f"  Offered   {r.offered} number{'' if r.offered == 1 else 's'} offered by the community")
    print(f"  Skipped   {len(bad)} invalid file{'' if len(bad) == 1 else 's'}")
    for path in bad[:10]:
        print(f"    {path}")
    if r.orphans:
        print(f"  Skipped {r.orphans} observation{'' if r.orphans == 1 else 's'} for menu options your tree does not have.")
    if r.future:
        print(f"  Skipped {r.future} observation{'' if r.future == 1 else 's'} dated in the future.")
    if r.truncated:
        print("  The community repo has more files than one update reads; some were not checked.")
    if r.capped:
        print("  Some companies have more community data than one update brings in; run Update again later.")
    return 0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="zeroout")
    sub = ap.add_subparsers(dest="command", required=True)
    sub.add_parser("setup", help="set up this computer", add_help=False)
    r = sub.add_parser("run", help="run the app on this computer")
    r.add_argument("--port", type=int, default=8000)
    r.add_argument("--no-browser", action="store_true", help="do not open the browser")
    sub.add_parser("crawl", help="map one company", add_help=False)
    c = sub.add_parser("contribute", help="share a mapped company with the community")
    c.add_argument("--company-id", type=int, required=True)
    c.add_argument("--yes", action="store_true",
                   help="skip the question and share at once, for scripts; the share is still public")
    sub.add_parser("update", help="fetch the community's trees")
    args, rest = ap.parse_known_args(argv)
    if args.command == "setup":
        from . import setup
        return setup.main(rest) or 0
    if args.command == "crawl":
        from . import crawl
        return crawl.main(rest) or 0
    if rest:
        ap.error(f"unrecognized arguments: {' '.join(rest)}")
    if args.command == "contribute":
        return contribute(args)
    if args.command == "update":
        return update(args)
    return run(args)


if __name__ == "__main__":
    sys.exit(main())
