from dataclasses import dataclass, field

VALID_KEYS = set("0123456789*#")
OUTCOMES = (
    "menu", "hold", "human", "voicemail", "hangup",
    "invalid_input", "needs_input", "speech_menu",
)
STATUSES = (
    "unexplored", "mapped", "changed", "stale",
    "human", "needs_input", "speech_menu",
)


@dataclass
class Option:
    key: str
    label: str


@dataclass
class Observation:
    prompt_text: str
    options: list[Option] = field(default_factory=list)
    outcome: str = "menu"
    prompt_seconds: float = 0.0


@dataclass
class Step:
    key: str
    wait: float
