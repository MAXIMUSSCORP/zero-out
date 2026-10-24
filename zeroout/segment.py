import io
import wave


def _open(wav: bytes) -> wave.Wave_read | None:
    try:
        return wave.open(io.BytesIO(wav), "rb")
    except (wave.Error, EOFError, ValueError):
        return None


def _clip(src: wave.Wave_read, start: int, end: int) -> bytes:
    src.setpos(start)
    frames = src.readframes(end - start)
    buf = io.BytesIO()
    with wave.open(buf, "wb") as out:
        out.setnchannels(src.getnchannels())
        out.setsampwidth(src.getsampwidth())
        out.setframerate(src.getframerate())
        out.writeframes(frames)
    return buf.getvalue()


def duration(wav: bytes) -> float:
    src = _open(wav)
    if src is None:
        return 0.0
    with src:
        rate = src.getframerate()
        return src.getnframes() / rate if rate else 0.0


def segment(wav: bytes, times: list[float]) -> list[bytes]:
    src = _open(wav)
    if src is None:
        return [b""] * (len(times) + 1)
    with src:
        total, rate = src.getnframes(), src.getframerate()
        cuts = sorted(min(max(round(t * rate), 0), total) for t in times)
        bounds = [0, *cuts, total]
        return [_clip(src, a, b) for a, b in zip(bounds, bounds[1:])]


def tail(wav: bytes, start: float) -> bytes:
    src = _open(wav)
    if src is None:
        return b""
    with src:
        total = src.getnframes()
        return _clip(src, min(max(round(start * src.getframerate()), 0), total), total)
