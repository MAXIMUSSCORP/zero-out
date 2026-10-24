import io
import struct
import wave

from zeroout.models import Observation, Step
from zeroout.probe import ProbeResult

RATE = 8000
MAGIC = 12345


def _wav(samples: list[int]) -> bytes:
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(RATE)
        w.writeframes(struct.pack(f"<{len(samples)}h", *samples))
    return buf.getvalue()


def _segment(path: str, seconds: float) -> list[int]:
    """MAGIC, ASCII bytes of the path, 0 terminator, then silence to `seconds`."""
    head = [MAGIC, *map(ord, path), 0]
    return head + [0] * max(round(seconds * RATE) - len(head), 0)


def decode(wav: bytes) -> str | None:
    """Path encoded at the start of a clip; "" for the root; None for garbage."""
    try:
        with wave.open(io.BytesIO(wav), "rb") as w:
            n = min(w.getnframes(), 64)
            s = struct.unpack(f"<{n}h", w.readframes(n))
    except (wave.Error, EOFError, struct.error):
        return None
    if not s or s[0] != MAGIC or 0 not in s[1:]:
        return None
    return bytes(s[1 : s.index(0, 1)]).decode("ascii", "replace")


class FakeMenu:
    def __init__(self, tree: dict[str, Observation]):
        self.tree = tree
        self.calls: list[str] = []

    def probe(self, number: str, steps: list[Step]) -> ProbeResult:
        path = ".".join(s.key for s in steps)
        self.calls.append(path)
        seconds = sum(s.wait for s in steps) + 1
        total = round(seconds * RATE)
        samples = [0] * (total - RATE) + _segment(path, 1)
        return ProbeResult(_wav(samples), seconds)

    def listen(self, wav: bytes) -> Observation | None:
        path = decode(wav)
        return None if path is None else self.tree.get(path)

    @staticmethod
    def recording(path: str, times: list[float]) -> bytes:
        keys = path.split(".") if path else []
        bounds = [0.0, *times]
        samples: list[int] = []
        for i, start in enumerate(bounds):
            end = bounds[i + 1] if i + 1 < len(bounds) else start + 1
            samples += _segment(".".join(keys[: min(i, len(keys))]), end - start)
        return _wav(samples)
