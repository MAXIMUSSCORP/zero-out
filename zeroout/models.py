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
    replays: bool | None = None  # the model's word that this option only repeats a menu or goes back; None = unknown


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


@dataclass
class Word:
    """A recognized word and when it was spoken, in seconds from the start of the recording."""
    text: str
    start: float
    end: float
