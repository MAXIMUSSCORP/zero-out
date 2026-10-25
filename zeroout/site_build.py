"""Build the static, read-only website for the community trees repo.

Stdlib only: this file is copied into zero-out-trees, where zeroout is not
installed. Every string in a tree file was typed or heard by a stranger, so each
value goes through esc() before it reaches a page, and no href is built from data
except the 10 digit company number.
"""
import html
import importlib.util
import json
import re
import sys
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path

CSP = ("default-src 'none'; style-src 'self'; script-src 'self'; img-src 'self'; "
       "base-uri 'none'; form-action 'none'")
NUMBER = re.compile(r"\d{10}")
PATH = re.compile(r"(\d{1,2}|[*#])(\.(\d{1,2}|[*#]))*")

CSS = """\
:root { color-scheme: light dark; }
body { font: 15px/1.5 system-ui, sans-serif; max-width: 52rem; margin: 2rem auto; padding: 0 1rem; }
h1 { font-size: 1.4rem; }
code, .mono { font-family: ui-monospace, monospace; }
.sub { opacity: .7; }
input#q { width: 100%; padding: .5rem; font: inherit; box-sizing: border-box; margin: 1rem 0; }
ul.companies { list-style: none; padding: 0; }
ul.companies li { padding: .5rem 0; border-bottom: 1px solid #8884; }
ul.companies li[hidden] { display: none; }
details { margin: .25rem 0 .25rem 1rem; }
details > summary { cursor: pointer; }
.key { font-family: ui-monospace, monospace; font-weight: 600; }
.badge { font: 11px ui-monospace, monospace; text-transform: uppercase; border: 1px solid currentColor; padding: 0 .35rem; border-radius: 3px; }
.changed { font-size: .85em; border-bottom: 1px dotted currentColor; }
.prompt { margin: .25rem 0; white-space: pre-wrap; }
.meta { font: 12px ui-monospace, monospace; opacity: .7; }
.note { margin-top: 2rem; opacity: .7; }
"""

JS = """\
(function () {
  var q = document.getElementById("q");
  if (!q) return;
  var items = document.querySelectorAll("ul.companies li");
  q.addEventListener("input", function () {
    var t = q.value.toLowerCase().trim();
    for (var i = 0; i < items.length; i++) {
      var hay = items[i].getAttribute("data-search") || "";
      items[i].hidden = t !== "" && hay.indexOf(t) === -1;
    }
  });
})();
"""


def esc(v):
    return html.escape(str(v), quote=True)


def norm(text):
    text = re.sub(r"[^\w\s]", "", str(text).lower())
    return " ".join(text.split())


def _shape(o):
    return (norm(o.get("prompt_text", "")),
            tuple(sorted(str(x.get("key", "")) for x in o.get("options", []))))


def _s(v):
    return v if isinstance(v, str) else ""


def _parse(body):
    """Return (doc, error). Only enough checking to render safely; check() does the rest."""
    try:
        d = json.loads(body)
    except ValueError as e:
        return None, [f"not valid JSON: {e}"]
    if not isinstance(d, dict):
        return None, ["top level is not an object"]
    if not isinstance(d.get("number"), str) or not NUMBER.fullmatch(d["number"]):
        return None, ["number must be 10 digits"]
    if not isinstance(d.get("observations"), list):
        return None, ["observations must be a list"]
    obs = []
    for o in d["observations"]:
        if not isinstance(o, dict) or not isinstance(o.get("path"), str):
            return None, ["observation without a path"]
        if o["path"] != "" and not PATH.fullmatch(o["path"]):
            return None, [f"bad path {o['path'][:20]!r}"]
        opts = o.get("options", [])
        if not isinstance(opts, list) or not all(isinstance(x, dict) for x in opts):
            return None, ["options must be a list of objects"]
        obs.append(o)
    d["observations"] = obs
    return d, []


def _company(entries, today):
    """entries: list of (order, doc). Returns the per-company model.

    The tree is walked from the root through the SHOWN options only, so a branch
    the company removed disappears, as it does in the app. Observations dated after
    today are ignored: a stranger can write any date into a file."""
    names = Counter(_s(d.get("name")) for _, d in entries if _s(d.get("name")))
    number = entries[0][1]["number"]
    name = min(names, key=lambda n: (-names[n], n)) if names else number
    by_path = {}
    seq = 0
    for _, d in sorted(entries, key=lambda e: (_s(e[1].get("contributed_at")), e[0])):
        for o in d["observations"]:
            seq += 1
            heard = _s(o.get("heard_at"))[:10]
            if heard > today:
                continue
            by_path.setdefault(o["path"], []).append((heard, seq, o))
    nodes = {}
    sources = {}

    def grow(path, depth=0):
        items = by_path.get(path)
        if not items or depth > 6:
            return None
        items.sort(key=lambda t: (t[0], t[1]))
        shown = items[-1][2]
        shape = _shape(shown)
        same = [i for i in items if _shape(i[2]) == shape]
        older = [i for i in items if _shape(i[2]) != shape]
        node = {"path": path, "o": shown, "heard": len(same), "last": items[-1][0],
                "changed": older[-1][0] if older else None, "src": {}, "kids": []}
        for _, _, o in items:
            src = _s(o.get("source")) or "unknown"
            node["src"][src] = node["src"].get(src, 0) + 1
        nodes[path] = node
        for key in dict.fromkeys(str(x.get("key", "")) for x in shown.get("options", [])):
            kid = grow(key if path == "" else f"{path}.{key}", depth + 1)
            if kid:
                node["kids"].append(kid)
        return node

    root = grow("")
    for n in nodes.values():
        for k, v in n["src"].items():
            sources[k] = sources.get(k, 0) + v
    latest = max((n["last"] for n in nodes.values()), default="")
    return {"name": name, "number": number, "top": [root] if root else [],
            "nodes": len(nodes), "latest": latest, "sources": sources}


def _srcs(d):
    return ", ".join(f"{esc(k)} {v}" for k, v in sorted(d.items()))


def _label_for(node, parent):
    if parent is None:
        return "Top menu" if node["path"] == "" else ""
    key = node["path"].rpartition(".")[2]
    for opt in parent["o"].get("options", []):
        if str(opt.get("key", "")) == key:
            return _s(opt.get("label"))
    return ""


def _node(node, parent, depth=0):
    o = node["o"]
    label = _label_for(node, parent)
    keys = node["path"].replace(".", " then ") if node["path"] else "start"
    out = [f'<details{" open" if depth == 0 else ""}><summary>'
           f'<span class="key">{esc(keys)}</span> {esc(label)} '
           f'<span class="badge">{esc(_s(o.get("outcome")) or "unknown")}</span> '
           f'<span class="meta">heard {node["heard"]} · last heard {esc(node["last"][:10] or "—")}</span>']
    if node["changed"]:
        out.append(f' <span class="changed">changed since {esc(node["changed"])}</span>')
    out.append("</summary>")
    out.append(f'<p class="prompt">{esc(_s(o.get("prompt_text")))}</p>')
    out.append(f'<p class="meta">sources: {_srcs(node["src"])}</p>')
    for k in node["kids"]:
        out.append(_node(k, node, depth + 1))
    out.append("</details>")
    return "".join(out)


def _page(title, body, prefix=""):
    return (f'<!doctype html>\n<html lang="en"><head><meta charset="utf-8">\n'
            f'<meta http-equiv="Content-Security-Policy" content="{CSP}">\n'
            f'<meta name="viewport" content="width=device-width, initial-scale=1">\n'
            f'<title>{esc(title)}</title>\n<link rel="stylesheet" href="{prefix}style.css">\n'
            f'</head><body>\n{body}\n<script src="{prefix}site.js"></script>\n</body></html>\n')


def _index(companies):
    if not companies:
        body = ('<h1>Zero-Out trees</h1>\n<p>No trees yet.</p>\n')
    else:
        rows = []
        for c in companies:
            search = esc(f"{c['name']} {c['number']}".lower())
            rows.append(f'<li data-search="{search}"><a href="c/{c["number"]}.html">{esc(c["name"])}</a> '
                        f'<span class="meta">{esc(c["number"])} · {c["nodes"]} nodes · '
                        f'last heard {esc(c["latest"][:10] or "—")}</span></li>')
        body = ('<h1>Zero-Out trees</h1>\n<p class="sub">Phone menus mapped by people running Zero-Out.</p>\n'
                '<input id="q" type="search" placeholder="Search by company or number" autocomplete="off">\n'
                '<ul class="companies">\n' + "\n".join(rows) + "\n</ul>")
    body += ('\n<p class="note">Read only. Every line was heard on a call and can be out of date; '
             'the changed marker shows where a menu has moved.</p>')
    return _page("Zero-Out trees", body)


def _company_page(c):
    body = (f'<p><a href="../index.html">All companies</a></p>\n<h1>{esc(c["name"])}</h1>\n'
            f'<p class="meta">{esc(c["number"])} · {c["nodes"]} nodes · '
            f'last heard {esc(c["latest"][:10] or "—")} · sources: {_srcs(c["sources"])}</p>\n')
    body += "\n".join(_node(n, None) for n in c["top"])
    return _page(c["name"], body, prefix="../")


def build(trees_dir, out_dir, *, check=None, today=None):
    """today is a date or YYYY-MM-DD string; it defaults to the UTC date."""
    trees_dir, out_dir = Path(trees_dir), Path(out_dir)
    today = (datetime.now(timezone.utc).date() if today is None else today)
    today = today if isinstance(today, str) else today.isoformat()
    root = trees_dir.resolve()
    files = sorted(p for p in trees_dir.rglob("*.json") if p.is_file() or p.is_symlink())
    invalid = {}
    by_number = {}
    for order, p in enumerate(files):
        rel = p.relative_to(trees_dir).as_posix()
        if p.is_symlink() or not p.resolve().is_relative_to(root):
            invalid[rel] = ["symlinks are not read"]
            continue
        try:
            raw = p.read_bytes()
            body = raw.decode("utf-8")
        except UnicodeDecodeError:
            invalid[rel] = ["not UTF-8"]
            continue
        errs = list(check("trees/" + rel, raw)) if check else []
        doc = None
        if not errs:
            doc, errs = _parse(body)
        if not errs and rel.split("/")[0] != doc["number"]:
            errs = ["file is not in the folder of its number"]
        if errs:
            invalid[rel] = errs
            continue
        by_number.setdefault(doc["number"], []).append((order, doc))
    companies = sorted((_company(e, today) for e in by_number.values()),
                       key=lambda c: (c["name"].lower(), c["name"], c["number"]))
    (out_dir / "c").mkdir(parents=True, exist_ok=True)
    for stale in (out_dir / "c").glob("*.html"):
        stale.unlink()
    (out_dir / "style.css").write_text(CSS, encoding="utf-8")
    (out_dir / "site.js").write_text(JS, encoding="utf-8")
    (out_dir / "index.html").write_text(_index(companies), encoding="utf-8")
    for c in companies:
        (out_dir / "c" / f"{c['number']}.html").write_text(_company_page(c), encoding="utf-8")
    return {"files": len(files), "invalid": invalid, "companies": len(companies),
            "nodes": sum(c["nodes"] for c in companies)}


def load_check():
    """The checker next to this file, loaded by path. Nothing on sys.path can stand in for it."""
    path = Path(__file__).with_name("community_check.py")
    if not path.is_file():
        return None
    spec = importlib.util.spec_from_file_location("_community_check_sibling", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod.check


def main(argv=None):
    argv = sys.argv[1:] if argv is None else argv
    strict = "--strict" in argv
    argv = [a for a in argv if a != "--strict"]
    if len(argv) != 2:
        print("  usage: python -m zeroout.site_build TREES_DIR OUT_DIR [--strict]")
        return 2
    check = load_check()
    if check is None:
        print("  error  community_check.py is missing next to site_build.py, refusing to build unchecked")
        return 2
    r = build(Path(argv[0]), Path(argv[1]), check=check)
    print(f"  files      {r['files']}")
    print(f"  companies  {r['companies']}")
    print(f"  nodes      {r['nodes']}")
    print(f"  invalid    {len(r['invalid'])}")
    for rel, errs in r["invalid"].items():
        for e in errs:
            print(f"  invalid  {rel}: {e}")
    return 1 if strict and r["invalid"] else 0


if __name__ == "__main__":
    sys.exit(main())
