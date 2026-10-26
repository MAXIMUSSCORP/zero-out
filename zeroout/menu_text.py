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
# A sentence that is only a key phrase ("Press 3.", "Press the star key.") has lost its words to the sentence before it.
_KEY_ONLY = re.compile(
    r"^(?:say\s+or\s+)?(?:" + _KEY.pattern + r")(?:\s+(?:key|button))?\s*[.!?]*$", re.IGNORECASE | re.ASCII)
_CHOICE_OPENER = re.compile(r"^(?:for|to|if|para|si)\b", re.IGNORECASE | re.ASCII)
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


_NOT_A_CHOICE = re.compile(
    r"^(?:thank|welcome|did\s+you\s+know|you\s+can|how\s+can\s+we)\b"
    r"|,\s+(?:you|we|they|it)\s+(?:may|can|will|could|should|might)\b", re.IGNORECASE | re.ASCII)


def _joins(prev):
    """Whether a key-only sentence belongs to the line before it: a question, or a For/If/Para/Si line, or a To
    line with no comma ("To protect your privacy, calls may be recorded." is not a choice). A greeting, a
    "did you know", or an If sentence that already has its own main clause is never a choice."""
    if _NOT_A_CHOICE.search(prev):
        return False
    if prev.rstrip().endswith("?"):
        return True
    opener = _CHOICE_OPENER.match(prev)
    if not opener:
        return False
    return opener.group(0).lower() != "to" or "," not in prev


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
            if _KEY_ONLY.match(clause) and lines and lines[-1]["key"] is None and _joins(lines[-1]["text"]):
                clause = lines.pop()["text"].rstrip(" .?!") + ", " + clause[:1].lower() + clause[1:]
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
_JOIN = r"(?:for|to|para|si|or|and|y|o)"
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


_CONDITION = re.compile(r"^(?:if|when|once|after)\b|\b(?:please|then)$", re.IGNORECASE | re.ASCII)
_KEY_WORD = re.compile(r"^\s*(?:key|button)\b", re.IGNORECASE | re.ASCII)
_WANT_TO = re.compile(
    r"^if\s+you(?:'d)?\s+(?:(?:would\s+)?(?:like|want|need|wish)\s+to\s+|have\s+"
    r"|are\s+calling\s+(?:about|for|regarding)\s+|are\s+(?:an?\s+)?)?"
    r"|^(?:are\s+you\s+calling\s+about|do\s+you\s+need|would\s+you\s+like|looking\s+for|do\s+you\s+have"
    r"|is\s+this\s+about)\s+", re.IGNORECASE | re.ASCII)
_PURPOSE = re.compile(r"^\s*(?:to|for|para)\b", re.IGNORECASE | re.ASCII)


_AT_ANY_TIME = re.compile(r"\bat\s+any\s+time\b", re.IGNORECASE | re.ASCII)


def _usable(s):
    return bool(s) and not _edges(_AT_ANY_TIME.sub("", s)) == "" and not _BARE.match(s) and not _KEY_PHRASE.search(s) and not _CONDITION.search(s)


def _derive(key, prompt_text):
    """The words that belong to this key, never the neighbouring option's. A sentence that starts with its key
    phrase ("Press 1 for billing and press 2 ...") puts each label after its key phrase, up to the next one;
    any other ("For billing, press 1, for support, press 2") puts it before, back to the previous one. A
    "condition, please press pound to return to ..." before the key is not a label, so the words after the key
    are. Of several sentences for one key, the one that ends on the key phrase is the plainest."""
    plain, other = [], []
    for line in menu_lines(prompt_text, [key]):
        if line["key"] != key:
            continue
        text = line["text"]
        found = list(_KEY_PHRASE.finditer(text))
        mine = next((i for i, m in enumerate(found) if _key_of(m) == key), None)
        if mine is None:
            continue
        end = found[mine + 1].start() if mine + 1 < len(found) else len(text)
        after = text[found[mine].end():end]
        if not text[:found[0].start()].strip(_EDGE):
            if _usable(_edges(after)):
                other.append(_edges(after))
            continue
        before = _edges(_WANT_TO.sub("", _edges(text[(found[mine - 1].end() if mine else 0):found[mine].start()])))
        if _usable(before):
            (other if _edges(_KEY_WORD.sub("", after)) else plain).append(before)
        elif _PURPOSE.match(after) and _usable(_edges(after)):
            other.append(_edges(after))
    return (plain or other or [""])[0]


# A navigation phrase that opens the label, after at most "if you want to". "Return to the previous menu" is a
# replay; "To start over with a new booking" and "main menu items on our website" are choices. A noun phrase
# ("the main menu") may be followed by a word or two; a bare verb ("go back", "start over") by nothing.
_LEAD = r"(?:if\s+you(?:'d)?\s+(?:would\s+)?(?:like|want|need)\s+to\s+|to\s+)?"
_NAV = [
    re.compile(_LEAD + r"(?:the\s+)?(?:main|previous|prior|last)\s+menu\b", re.IGNORECASE | re.ASCII),
    re.compile(_LEAD + r"(?:go|return)\s+(?:back\s+)?(?:to\s+)?(?:the\s+)?(?:main|previous|prior|last)\s+menu\b",
               re.IGNORECASE | re.ASCII),
    re.compile(_LEAD + r"(?:go|return)\s+(?:back\s+)?(?:to\s+)?(?:the\s+)?(?:main|previous|prior|last)\b(?!\s+menu)",
               re.IGNORECASE | re.ASCII),
    re.compile(_LEAD + r"(?:volver\s+al\s+men[uúÚ]|men[uúÚ]\s+(?:anterior|principal))(?![a-z])",
               re.IGNORECASE | re.ASCII),
]
_NAV_BARE = re.compile(_LEAD + r"(?:go\s+back|start(?:ing)?\s+over|volver)$", re.IGNORECASE | re.ASCII)
_REPEAT = re.compile(
    _LEAD + r"(?:repeat\s+(?:(?:this|that|these|those|the)\s+)?(?:information|options?|menu|message|choices)\b"
    r"|repeat$|hear\s+(?:(?:these|this|the|those|that)\s+)?(?:options|menu|information|message|choices)\s+again\b"
    r"|(?:say|hear|play)\s+(?:that|this|it)\s+again\b"
    r"|repetir\s+(?:(?:este|esta|estas|estos|el|la|las|los)\s+)?"
    r"(?:men[uúÚ](?![a-z])|(?:opciones|mensaje|informaci[oóÓ]n)\b)|repetir$)",
    re.IGNORECASE | re.ASCII)
_LEAD_FILLER = re.compile(r"^(?:at\s+any\s+time\b[\s,]*)", re.IGNORECASE | re.ASCII)
_TAIL_FILLER = re.compile(
    r"(?:[\s,]+or\s+to\s+hear\s+(?:them|it|that|this)\s+again|[\s,]+at\s+any\s+time|\s+(?:now|again))$",
    re.IGNORECASE | re.ASCII)
_CUT = re.compile(r"\s+(?:and|then)\s+", re.IGNORECASE | re.ASCII)


def _navigates(derived):
    """Whether a derived label opens with a navigation phrase. Filler ("at any time", "now", "or to hear them
    again") is dropped first, and the label is cut at " and " or " then ", since what follows is another act."""
    text = _CUT.split(derived.strip(), 1)[0]
    stripped = text
    while True:
        t = _TAIL_FILLER.sub("", _LEAD_FILLER.sub("", stripped)).strip(_EDGE)
        if t == stripped:
            break
        stripped = t
    for c in (text, stripped):
        if len(c.split()) > 6:
            continue
        if _NAV_BARE.match(c):
            return True
        for rx, extra in [(r, 2) for r in _NAV[:2] + _NAV[3:] + [_REPEAT]] + [(_NAV[2], 0)]:
            m = rx.match(c)
            if m and len(c[m.end():].split()) <= extra:
                return True
    return False


def option_replays(stored_label, key, prompt_text) -> bool:
    """Whether an option only replays a menu. The words the transcript gives that key decide, and only when
    a navigation phrase opens them and they are short; the stored label can be shifted by a model, so it is
    read only when nothing can be derived. Never raises."""
    try:
        derived = _derive(str(key), prompt_text) if isinstance(prompt_text, str) else ""
        if derived:
            return _navigates(derived)
        return (isinstance(stored_label, str) and not _BARE.match(stored_label.strip(" .,;:!?"))
                and replays_menu(stored_label))
    except Exception:
        return False


_STOP = frozenset(
    "a an the of to for and or in on at with your you our is are be about please press if from this that it as by"
    " para el la los las de y o un una con su sus".split())


def _content(text):
    return {w for w in re.findall(r"[a-z0-9']+", text.lower(), re.ASCII | re.IGNORECASE) if w not in _STOP}


def _derived_by_key(prompt_text):
    return {k: _derive(k, prompt_text) for k in {x["key"] for x in menu_lines(prompt_text) if x["key"]}}


def _stored_loses(stored, key, derived, prompt_text):
    """Whether the words from the transcript replace a real stored label: they do when the two share a content
    word (case and punctuation aside), or when the stored label shares one with another key's derived label,
    which is a model's shifted pairing. Sharing with its own is enough to keep the key's own derived label. A
    merged label of more than twelve words never overrides a stored one on a shared word with itself alone."""
    mine = _content(stored)
    if not mine:
        return False
    if mine & _content(derived) and len(derived.split()) <= 12:
        return True
    return any(k != key and mine & _content(other) for k, other in _derived_by_key(prompt_text).items())


def fill_label(label, key, prompt_text) -> str:
    """An option's label. The words the prompt gives the key ("For Costco Travel, press 5." gives "Costco
    Travel") replace an empty or key only stored label, and a stored one that shares a word with them or that
    belongs to another key; otherwise the stored label stands, or "Option <key>" when there is none. Never raises."""
    try:
        key = str(key)
        text = label if isinstance(label, str) else ""
        real = bool(text.strip()) and not _BARE.match(text.strip(" .,;:!?"))
        derived = _derive(key, prompt_text) if isinstance(prompt_text, str) else ""
        if derived and (not real or _stored_loses(text, key, derived, prompt_text)):
            return derived[:1].upper() + derived[1:]
        return text if real else f"Option {key}"
    except Exception:
        return label if isinstance(label, str) else ""


_TRIGGER = re.compile(
    r"\b(?:you\s+can\s+say(?:\s+things\s+like)?|say\s+something\s+like|for\s+example,?(?:\s+say)?"
    r"|try\s+saying|or\s+say|puede\s+decir(?:\s+cosas\s+como)?|por\s+ejemplo,?|diga)\b[:,]?",
    re.IGNORECASE | re.ASCII)
_EG_SPLIT = re.compile(r"\s*;\s*(?:or\s+)?|\s*,\s*or\s*,?\s*|\s+or\s+|\s*,\s*o\s*,\s*|\s+o\s+", re.IGNORECASE | re.ASCII)
_QUOTES = "\"'“”‘’«»`"
_NOT_EXAMPLE = re.compile(r"\d|\[")
MAX_EXAMPLES = 5
MAX_EXAMPLE_CHARS = 80


def spoken_examples(text: str) -> list[str]:
    """The phrases a voice menu suggests saying ("You can say things like, I need help, or, book a flight."),
    at most five, each at most 80 characters. Never raises."""
    try:
        if not isinstance(text, str):
            return []
        out = []
        for sentence in _sentences(" ".join(text[:4000].split())):
            found = list(_TRIGGER.finditer(sentence))
            for i, m in enumerate(found):
                end = found[i + 1].start() if i + 1 < len(found) else len(sentence)
                for part in _EG_SPLIT.split(sentence[m.end():end]):
                    p = part.strip(" \t" + _QUOTES).rstrip(" .,;:!?" + _QUOTES).lstrip(_QUOTES + " ")
                    if len(p.split()) < 2 or _NOT_EXAMPLE.search(p):
                        continue
                    p = (p[0].upper() + p[1:])[:MAX_EXAMPLE_CHARS].rstrip()
                    if p.lower() not in {x.lower() for x in out}:
                        out.append(p)
        return out[:MAX_EXAMPLES]
    except Exception:
        return []
