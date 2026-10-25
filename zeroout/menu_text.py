"""Menu words, one line each: what a phone menu said, split so every sentence and every option stands alone.

Stdlib only and no imports from zeroout: this file is copied into zero-out-trees and loaded by path.
Text arrives from strangers and from a speech model, so nothing here may raise.
"""
import re
from collections.abc import Iterable

_WORDS = {
    "zero": "0", "one": "1", "two": "2", "three": "3", "four": "4", "five": "5", "six": "6",
    "seven": "7", "eight": "8", "nine": "9", "star": "*", "pound": "#",
    "cero": "0", "uno": "1", "dos": "2", "tres": "3", "cuatro": "4", "cinco": "5", "seis": "6",
    "siete": "7", "ocho": "8", "nueve": "9", "asterisco": "*", "numeral": "#", "gato": "#",
}
_VERBS = r"(?:press|push|dial|select|enter|oprima|marque|presione)"
_KEY = re.compile(
    rf"\b{_VERBS}\s+(?:the\s+)?(?:(?:number|digit|key)\s+)?"
    rf"(?:(?P<d>[0-9])(?![0-9])|(?P<w>{'|'.join(_WORDS)})\b(?!\s+(?:of|de)\b)|(?P<s>[*#]))",
    re.IGNORECASE | re.ASCII)
_END = re.compile(r"""(?<![.?!])[.?!]++["')\]]*+(?=\s|$)""")
_STRONG = re.compile(r"\s*[,;]\s*(?:or\s+)?", re.IGNORECASE | re.ASCII)
_OR = re.compile(r"\s+or\s+", re.IGNORECASE | re.ASCII)


def _key_of(m):
    if m.group("d"):
        return m.group("d")
    if m.group("s"):
        return m.group("s")
    return _WORDS.get(m.group("w").lower())


def _sentences(text):
    out, start = [], 0
    for m in _END.finditer(text):
        out.append(text[start:m.end()])
        start = m.end()
    out.append(text[start:])
    return [s.strip() for s in out if s.strip()]


def _clauses(sentence):
    """Split before each key phrase after the first, at the comma, semicolon or ' or ' that starts its clause."""
    hits = list(_KEY.finditer(sentence))
    cuts = []
    for prev, cur in zip(hits, hits[1:]):
        span = sentence[prev.end():cur.start()]
        sep = _STRONG.search(span) or _OR.search(span)
        if sep:
            cuts.append((prev.end() + sep.start(), prev.end() + sep.end()))
    out, start = [], 0
    for a, b in cuts:
        out.append(sentence[start:a])
        start = b
    out.append(sentence[start:])
    return [c.strip(" ,;") for c in out if c.strip(" ,;")]


def menu_lines(text: str, keys: Iterable[str] = ()) -> list[dict]:
    """[{"text", "key"}] one per line. key is the key the line asks for; with keys given, any other is None."""
    if not isinstance(text, str):
        return []
    try:
        allowed = {str(k) for k in keys}
    except TypeError:
        allowed = set()
    lines = []
    for sentence in _sentences(" ".join(text.split())):
        for clause in _clauses(sentence):
            key = None
            for m in _KEY.finditer(clause):
                k = _key_of(m)
                if k and (not allowed or k in allowed):
                    key = k
                    break
            lines.append({"text": clause, "key": key})
    return lines
