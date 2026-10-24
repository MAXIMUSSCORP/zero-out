import io
import wave

import pytest

from zeroout.segment import duration, segment, tail


def make_wav(seconds: float, rate: int = 8000, channels: int = 1, width: int = 2) -> bytes:
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(channels)
        w.setsampwidth(width)
        w.setframerate(rate)
        w.writeframes(b"\x00" * int(seconds * rate) * channels * width)
    return buf.getvalue()


def params(wav: bytes) -> tuple[int, int, int]:
    with wave.open(io.BytesIO(wav)) as w:
        return w.getnchannels(), w.getsampwidth(), w.getframerate()


def test_segment_splits_at_times():
    clips = segment(make_wav(10), [3, 7])
    assert [duration(c) for c in clips] == pytest.approx([3, 4, 3], abs=0.01)


def test_tail_returns_remainder():
    assert duration(tail(make_wav(10), 7)) == pytest.approx(3, abs=0.01)


def test_duration_empty_and_garbage():
    assert duration(b"") == 0.0
    assert duration(b"garbage") == 0.0


def test_times_beyond_end_are_clamped():
    clips = segment(make_wav(10), [4, 50])
    assert len(clips) == 3
    assert [duration(c) for c in clips] == pytest.approx([4, 6, 0], abs=0.01)


def test_negative_times_clamped_to_zero():
    clips = segment(make_wav(10), [-5, 4])
    assert [duration(c) for c in clips] == pytest.approx([0, 4, 6], abs=0.01)


def test_unsorted_times():
    clips = segment(make_wav(10), [7, 3])
    assert [duration(c) for c in clips] == pytest.approx([3, 4, 3], abs=0.01)


def test_empty_times_returns_whole_clip():
    clips = segment(make_wav(10), [])
    assert len(clips) == 1
    assert duration(clips[0]) == pytest.approx(10, abs=0.01)


def test_stereo_16bit_preserves_params():
    src = make_wav(4, rate=16000, channels=2, width=2)
    for clip in segment(src, [1, 3]) + [tail(src, 2)]:
        assert params(clip) == (2, 2, 16000)


def test_zero_length_clips_are_valid_wav():
    clips = segment(make_wav(2), [0, 2])
    assert [duration(c) for c in clips] == [0.0, 2.0, 0.0]
    assert all(params(c) == (1, 2, 8000) for c in clips)


def test_tail_past_end_is_zero_seconds():
    clip = tail(make_wav(10), 99)
    assert clip != b""
    assert duration(clip) == 0.0


def test_tail_negative_start_returns_whole():
    assert duration(tail(make_wav(10), -3)) == pytest.approx(10, abs=0.01)


def test_tail_invalid_input_returns_empty():
    assert tail(b"", 1) == b""
    assert tail(b"garbage", 1) == b""


def test_segment_invalid_input_returns_empty_clips():
    assert segment(b"garbage", [1, 2]) == [b"", b"", b""]
