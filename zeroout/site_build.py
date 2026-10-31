"""Build the static, read-only website for the community trees repo.

Stdlib only: this file is copied into zero-out-trees, where zeroout is not
installed. Every string in a tree file was typed or heard by a stranger, so each
value goes through esc() before it reaches a page, and no href is built from data
except the 10 digit company number.
"""
import base64
import hashlib
import html
import functools
import importlib.util
import json
import re
import sys
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path

NUMBER = re.compile(r"\d{10}")
PATH = re.compile(r"(\d{1,2}|[*#])(\.(\d{1,2}|[*#]))*")

CSS = """\
:root{
  --g1:#FCFCFC;--g2:#F8F8F8;--g3:#F1F1F1;--g4:#E9E9E9;--g5:#E1E1E1;--g6:#D8D8D8;
  --g7:#CBCBCB;--g8:#B4B4B4;--g9:#8C8C8C;--g10:#7A7A7A;--g11:#606060;--g12:#1B1B1B;
  --inv-bg:#000;--inv-fg:#FFF;
  color-scheme:light;
}
@media (prefers-color-scheme:dark){:root:not([data-theme="light"]){
  --g1:#0A0A0A;--g2:#101010;--g3:#181818;--g4:#202020;--g5:#282828;--g6:#303030;
  --g7:#3A3A3A;--g8:#4C4C4C;--g9:#6C6C6C;--g10:#7C7C7C;--g11:#A2A2A2;--g12:#EFEFEF;
  --inv-bg:#FFF;--inv-fg:#000;
  color-scheme:dark;
}}
:root[data-theme="dark"]{
  --g1:#0A0A0A;--g2:#101010;--g3:#181818;--g4:#202020;--g5:#282828;--g6:#303030;
  --g7:#3A3A3A;--g8:#4C4C4C;--g9:#6C6C6C;--g10:#7C7C7C;--g11:#A2A2A2;--g12:#EFEFEF;
  --inv-bg:#FFF;--inv-fg:#000;
  color-scheme:dark;
}
:root{
  --app:var(--g1);--tile:var(--g3);--surface:var(--g2);--line:var(--g6);--line-int:var(--g7);
  --text:var(--g12);--text-low:var(--g11);--text-dim:var(--g9);
  --sans:Inter,-apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,Helvetica,Arial,sans-serif;
}
*{box-sizing:border-box}
body{margin:0;background:var(--app);color:var(--text);font:17px/1.47 var(--sans);
  font-variant-numeric:tabular-nums;-webkit-font-smoothing:antialiased}
a{color:inherit}
.bar{position:sticky;top:0;z-index:2;display:flex;align-items:center;justify-content:space-between;
  height:48px;padding:0 22px;background:var(--app);
  background:color-mix(in srgb,var(--app) 82%,transparent);
  -webkit-backdrop-filter:saturate(180%) blur(20px);backdrop-filter:saturate(180%) blur(20px);
  border-bottom:1px solid var(--line)}
.bar a{text-decoration:none;font-size:14px;font-weight:600;letter-spacing:-.01em}
.bar span{font-size:14px;color:var(--text-low)}
.page{max-width:980px;margin:0 auto;padding:0 22px 64px}
.hero{padding:72px 0 32px}
h1.display{margin:0;font-size:56px;line-height:1.07;font-weight:700;letter-spacing:-.03em}
.lede{margin:16px 0 0;max-width:640px;font-size:21px;line-height:1.38;color:var(--text-low);letter-spacing:-.01em}
.back{display:inline-block;margin:28px 0 0;font-size:14px;color:var(--text-low)}
.meta{font-size:14px;color:var(--text-low)}
input#q{display:block;width:100%;height:44px;margin:0 0 20px;padding:0 14px;border:1px solid var(--line);
  border-radius:12px;background:var(--surface);color:var(--text);font:inherit}
input#q:focus{outline:2px solid var(--g8);outline-offset:1px}
.list{list-style:none;margin:0;padding:0;background:var(--surface);border:1px solid var(--line);
  border-radius:16px;overflow:hidden}
.list li{border-top:1px solid var(--line)}
.list li:first-child{border-top:0}
.list li[hidden]{display:none}
.list a{display:flex;flex-wrap:wrap;align-items:baseline;justify-content:space-between;gap:4px 16px;
  min-height:56px;padding:12px 18px;text-decoration:none}
.list a:hover{background:var(--tile)}
.list .name{font-weight:600;letter-spacing:-.01em}
details{margin:0}
details details{margin:4px 0 4px 20px;padding-left:16px;border-left:1px solid var(--line)}
details>summary{display:flex;flex-wrap:wrap;align-items:center;gap:6px 12px;min-height:44px;cursor:pointer;list-style:none}
details>summary::-webkit-details-marker{display:none}
details>summary::before{content:"\\25B8";color:var(--text-dim);width:14px}
details[open]>summary::before{content:"\\25BE"}
.menu{background:var(--surface);border:1px solid var(--line);border-radius:16px;padding:6px 18px;margin:0 0 12px}
.menu>details>summary{font-weight:600;letter-spacing:-.01em}
.keys{display:inline-flex;align-items:center;gap:6px}
.then{font-size:14px;color:var(--text-dim)}
.key{display:inline-flex;align-items:center;justify-content:center;flex:none;width:32px;height:32px;
  border-radius:10px;background:var(--g4);border:1px solid var(--line-int);color:var(--text);
  font-size:17px;font-weight:600;line-height:1}
.key.sm{font-size:15px}
.key.lit{background:var(--inv-bg);border-color:var(--inv-bg);color:var(--inv-fg)}
.state{font-size:14px;color:var(--text-low)}
.state::before{content:"\\25CF";margin-right:6px;font-size:11px;vertical-align:1px}
.changed{font-size:14px;border-bottom:1px dotted currentColor}
.said{margin:4px 0 10px 0;padding-left:14px;border-left:2px solid var(--line-int);color:var(--text-low)}
.lbl{margin:0 0 2px;font-size:12px;color:var(--text-dim)}
.lines{list-style:none;margin:0;padding:0}
.lines li{padding:1px 0;overflow-wrap:anywhere;max-width:620px}
.voice{margin:0 0 10px;padding:12px 16px;border-radius:14px;background:var(--tile);font-size:14px}
.eg{margin:0 0 10px;padding:0 0 0 18px;color:var(--text-low);font-size:14px}
.tip{margin:0 0 10px;font-size:14px;color:var(--text-low)}
.tip .lbl{display:inline;margin-right:6px}
.voice-tag{flex-basis:100%}
.opts{margin:0 0 8px;padding:0;list-style:none}
.opts li{display:flex;align-items:center;gap:12px;padding:3px 0}
.opts .t{overflow-wrap:anywhere}
.src{margin:0 0 12px;font-size:14px;color:var(--text-low)}
.note{margin:40px 0 0;padding:20px 22px;background:var(--tile);border-radius:16px;color:var(--text-low);
  font-size:14px;line-height:1.5;max-width:692px}
@media (max-width:734px){
  .page{padding:0 16px 48px}.bar{padding:0 16px}
  .hero{padding:44px 0 24px}
  h1.display{font-size:40px}
  .lede{font-size:17px}
  details details{margin-left:8px;padding-left:10px}
}
"""

CSP = ("default-src 'none'; style-src 'sha256-" + base64.b64encode(hashlib.sha256(CSS.encode("utf-8")).digest()).decode()
       + "'; script-src 'self'; img-src 'self'; base-uri 'none'; form-action 'none'")

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
            "nodes": len(nodes), "latest": latest, "sources": sources,
            "voice": bool(root) and _voice(root["o"])}


def _srcs(d):
    return ", ".join(f"{esc(k)} {v}" for k, v in sorted(d.items()))


def _label_for(node, parent):
    if parent is None:
        return "Top menu" if node["path"] == "" else ""
    key = node["path"].rpartition(".")[2]
    for opt in parent["o"].get("options", []):
        if str(opt.get("key", "")) == key:
            return _fill(_s(opt.get("label")), key, _s(parent["o"].get("prompt_text")))
    return ""


VOICE = "Voice menu: you say what you need; there are no keys to press."
TIP = ('To reach a person, try saying "representative", "agent" or "operator". '
       'If that doesn\'t work, press 0 or say "Transcription Unavailable".')
KEY_ORDER = "1234567890*#"


def _voice(o):
    return "speech_menu" in (_s(o.get("outcome")), _s(o.get("status")))


def _key_rank(key):
    """1 to 9, then 0, then * and #; anything else after them, by text."""
    return (KEY_ORDER.index(key), "") if len(key) == 1 and key in KEY_ORDER else (len(KEY_ORDER), key)


def _cap(key, cls="key"):
    return f'<span class="{cls}">{esc(key)}</span>'


def _node(node, parent, lines, depth=0):
    o = node["o"]
    label = _label_for(node, parent)
    path = node["path"]
    if path:
        parts = path.split(".")
        caps = " <span class=\"then\">then</span> ".join(
            _cap(k, "key lit" if i == len(parts) - 1 else "key") for i, k in enumerate(parts))
        head = f'<span class="keys">{caps}</span> {esc(label)}'
    else:
        head = "Top menu"
    out = [f'<details{" open" if depth == 0 else ""}><summary>{head} '
           f'<span class="state">{esc(_s(o.get("outcome")) or "unknown")}</span> '
           f'<span class="meta">heard {node["heard"]} · last heard {esc(node["last"][:10] or "—")}</span>']
    if node["changed"]:
        out.append(f' <span class="changed">changed since {esc(node["changed"])}</span>')
    out.append("</summary>")
    rows = [f'<li><span class="t">{esc(ln["text"])}</span></li>' for ln in lines(_s(o.get("prompt_text")))]
    if rows:
        out.append('<div class="said"><p class="lbl">What the menu says</p><ol class="lines">' + "".join(rows) + "</ol></div>")
    if _voice(o):
        out.append(f'<p class="voice">{VOICE}</p>')
        eg = _examples(_s(o.get("prompt_text")))
        if eg:
            out.append('<p class="lbl">This line suggests saying:</p><ul class="eg">'
                       + "".join(f"<li>{esc(x)}</li>" for x in eg) + "</ul>")
        out.append(f'<p class="tip"><span class="lbl">General tip</span> {esc(TIP)}</p>')
    else:
        replays, prompt = _replays(), _s(o.get("prompt_text"))
        pairs = [(str(x.get("key", "")), x) for x in o.get("options", [])]
        labels = {k: _fill(_s(x.get("label")), k, prompt) for k, x in pairs}
        opts = [f'<li>{_cap(k, "key sm")}<span class="t">{esc(labels[k])}</span>'
                f'{REPLAY_STATE if replays(_s(x.get("label")), k, prompt) else ""}</li>'
                for k, x in sorted(((str(x.get("key", "")), x) for x in o.get("options", [])),
                                   key=lambda kx: _key_rank(kx[0]))]
        if opts:
            out.append('<ul class="opts">' + "".join(opts) + "</ul>")
    out.append(f'<p class="src">Heard by: {_srcs(node["src"])}</p>')
    for k in node["kids"]:
        out.append(_node(k, node, lines, depth + 1))
    out.append("</details>")
    return "".join(out)


NOTE = ('<p class="note">This site is read only. It is built from files that people chose to share, '
        'and nothing on it is verified. A menu can be out of date; the changed marker shows where one has moved.</p>')


def _page(title, body, prefix=""):
    home = f"{prefix}index.html"
    return (f'<!doctype html>\n<html lang="en"><head><meta charset="utf-8">\n'
            f'<meta http-equiv="Content-Security-Policy" content="{CSP}">\n'
            f'<meta name="viewport" content="width=device-width, initial-scale=1">\n'
            f'<title>{esc(title)}</title>\n<style>{CSS}</style>\n'
            f'</head><body>\n<header class="bar"><a href="{home}">Zero-Out</a><span>Phone menus</span></header>\n'
            f'<main class="page">\n{body}\n{NOTE}\n</main>\n<script src="{prefix}site.js"></script>\n</body></html>\n')


def _index(companies):
    hero = ('<section class="hero"><h1 class="display">Phone menus, mapped by people.</h1>\n'
            '<p class="lede">Which key to press to reach a person, written down by people who called.</p></section>')
    if not companies:
        body = hero + '\n<p>No menus yet.</p>'
    else:
        rows = []
        for c in companies:
            search = esc(f"{c['name']} {c['number']}".lower())
            rows.append(f'<li data-search="{search}"><a href="c/{c["number"]}.html"><span class="name">{esc(c["name"])}</span> '
                        f'<span class="meta">{esc(c["number"])} · {c["nodes"]} menus · '
                        f'last heard {esc(c["latest"][:10] or "—")}</span>'
                        + ('<span class="meta voice-tag">Voice menu, no keys</span>' if c["voice"] else "")
                        + '</a></li>')
        body = (hero + '\n<input id="q" type="search" placeholder="Search by company or number" autocomplete="off">\n'
                '<ul class="list companies">\n' + "\n".join(rows) + "\n</ul>")
    return _page("Zero-Out phone menus", body)


def _company_page(c, lines):
    body = (f'<a class="back" href="../index.html">All companies</a>\n'
            f'<section class="hero"><h1 class="display">{esc(c["name"])}</h1>\n'
            f'<p class="lede">{esc(c["number"])} · {c["nodes"]} menus · '
            f'last heard {esc(c["latest"][:10] or "—")} · heard by: {_srcs(c["sources"])}</p></section>\n')
    body += "\n".join(f'<div class="menu">{_node(n, None, lines)}</div>' for n in c["top"])
    return _page(c["name"], body, prefix="../")


def build(trees_dir, out_dir, *, check=None, today=None, lines=None):
    """today is a date or YYYY-MM-DD string; it defaults to the UTC date. lines is menu_lines, loaded from the sibling file."""
    lines = lines or load_menu_lines()
    if lines is None:
        raise FileNotFoundError("menu_text.py is missing next to site_build.py")
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
    (out_dir / "style.css").unlink(missing_ok=True)
    (out_dir / "site.js").write_text(JS, encoding="utf-8")
    (out_dir / "index.html").write_text(_index(companies), encoding="utf-8")
    for c in companies:
        (out_dir / "c" / f"{c['number']}.html").write_text(_company_page(c, lines), encoding="utf-8")
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


REPLAY_STATE = ' <span class="state">Replays a menu</span>'


@functools.lru_cache(maxsize=1)
def _menu_text():
    """The sibling menu_text.py loaded by path, or None when it is missing."""
    path = Path(__file__).with_name("menu_text.py")
    if not path.is_file():
        return None
    spec = importlib.util.spec_from_file_location("_menu_text_replays", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _replays():
    """option_replays from menu_text.py; with no sibling, nothing is marked."""
    mod = _menu_text()
    return mod.option_replays if mod else (lambda label, key, prompt_text: False)


def _examples(prompt_text):
    """spoken_examples from menu_text.py; with no sibling, none."""
    mod = _menu_text()
    return mod.spoken_examples(prompt_text) if mod else []


def _fill(label, key, prompt_text):
    """An empty or key only label made from the prompt sentence for that key; as it is without menu_text.py."""
    mod = _menu_text()
    return mod.fill_label(label, key, prompt_text) if mod else label


def load_menu_lines():
    """menu_lines from the sibling menu_text.py, loaded by path the same way."""
    path = Path(__file__).with_name("menu_text.py")
    if not path.is_file():
        return None
    spec = importlib.util.spec_from_file_location("_menu_text_sibling", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod.menu_lines


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
    if load_menu_lines() is None:
        print("  error  menu_text.py is missing next to site_build.py")
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
