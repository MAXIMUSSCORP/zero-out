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


# Options that only replay a menu already heard. Recognized by the label, never the key, because
# some companies use * or # for real choices.
_REPLAY = re.compile(
    r"\brepeat\s+(?:(?:this|that|these|those|the)\s+)?(?:information|options?|menu|message|choices)\b"
    r"|\brepeat\s*$"
    r"|\bhear\s+(?:(?:these|this|the|those|that)\s+)?(?:options|menu|information|message|choices)\s+again\b"
    r"|\b(?:say|hear|play)\s+(?:that|this|it)\s+again\b"
    r"|\b(?:previous|prior|last)\s+menu\b|\bmain\s+menu\b"
    r"|\bgo\s+back\b|\bstart(?:ing)?\s+over\b"
    r"|\breturn\s+to\s+(?:the\s+)?(?:previous|prior|last|main)\b"
    r"|\bvolver\s+a\s+(?:escuchar|o[ií]r)\b|\bvolver\s+al\s+men[uúÚ](?![a-z])"
    r"|\brepetir\s+(?:(?:este|esta|estas|estos|el|la|las|los)\s+)?"
    r"(?:men[uúÚ](?![a-z])|(?:opciones|mensaje|informaci[oóÓ]n)\b)"
    r"|\b(?:repetir|volver)\s*$|\bmen[uúÚ]\s+(?:anterior|principal)\b",
    re.IGNORECASE | re.ASCII)


def replays_menu(label) -> bool:
    """True when an option's label is navigation (repeat, previous menu, main menu, start over)."""
    try:
        return isinstance(label, str) and _REPLAY.search(label) is not None
    except Exception:
        return False


# A label that is only the key (or its phrase) says nothing; the words that belong to the key are in the
# sentence of the prompt that asks for it.
_BARE = re.compile(
    rf"^(?:say\s+or\s+)?(?:{_VERBS}\s+)?(?:the\s+)?(?:(?:number|digit|key)\s+)?"
    rf"(?:[0-9]|{'|'.join(_WORDS)}|hash|[*#])$",
    re.IGNORECASE | re.ASCII)
_KEY_PHRASE = re.compile(r"(?:\bsay\s+or\s+)?" + _KEY.pattern, re.IGNORECASE | re.ASCII)
_EDGE = " ,;.:!?-"
_JOIN = r"(?:for|to|para|or|and|y|o|a)"
_LEAD_WORD = re.compile(rf"^{_JOIN}\s+", re.IGNORECASE | re.ASCII)
_TAIL_WORD = re.compile(rf"\s+{_JOIN}$", re.IGNORECASE | re.ASCII)


def _edges(text):
    """A stretch of a sentence with the punctuation and joining words (and, or, for, to) trimmed off both ends."""
    s = text.strip(_EDGE)
    while True:
        t = _TAIL_WORD.sub("", _LEAD_WORD.sub("", s)).strip(_EDGE)
        if t == s:
            return s
        s = t


def _derive(key, prompt_text):
    """The words that belong to this key, never the neighbouring option's. A sentence that starts with its key
    phrase ("Press 1 for billing and press 2 ...") puts each label after its key phrase, up to the next one;
    any other ("For billing, press 1, for support, press 2") puts it before, back to the previous one."""
    for line in menu_lines(prompt_text, [key]):
        if line["key"] != key:
            continue
        text = line["text"]
        found = list(_KEY_PHRASE.finditer(text))
        mine = next((i for i, m in enumerate(found) if _key_of(m) == key), None)
        if mine is None:
            continue
        if not text[:found[0].start()].strip(_EDGE):
            end = found[mine + 1].start() if mine + 1 < len(found) else len(text)
            side = text[found[mine].end():end]
        else:
            start = found[mine - 1].end() if mine else 0
            side = text[start:found[mine].start()]
        s = _edges(side)
        if s and not _BARE.match(s) and not _KEY_PHRASE.search(s):
            return s
    return ""


def fill_label(label, key, prompt_text) -> str:
    """An option's label, with an empty or key only one replaced by the words its prompt sentence gives it
    ("For Costco Travel, press 5." gives "Costco Travel"), else "Option <key>". Never raises."""
    try:
        text = label if isinstance(label, str) else ""
        if text.strip() and not _BARE.match(text.strip(" .,;:!?")):
            return text
        key = str(key)
        derived = _derive(key, prompt_text) if isinstance(prompt_text, str) else ""
        return derived or f"Option {key}"
    except Exception:
        return label if isinstance(label, str) else ""
