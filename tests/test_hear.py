import hashlib
import io
import struct
import wave

import numpy as np
import pytest

from zeroout import hear
from zeroout.hear import decode, transcribe, words_from_tokens
from zeroout.models import Word


def make_wav(frames: bytes, *, rate: int = 8000, channels: int = 1, width: int = 2) -> bytes:
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(channels)
        w.setsampwidth(width)
        w.setframerate(rate)
        w.writeframes(frames)
    return buf.getvalue()


def pcm16(*samples: int) -> bytes:
    return struct.pack(f"<{len(samples)}h", *samples)


def test_decode_16bit_mono():
    audio, rate = decode(make_wav(pcm16(0, 16384, -16384, 32767)))
    assert rate == 8000 and audio.dtype == np.float32
    assert audio.tolist() == pytest.approx([0.0, 0.5, -0.5, 32767 / 32768])


def test_decode_stereo_averaged():
    audio, _ = decode(make_wav(pcm16(16384, -16384, 16384, 16384), channels=2))
    assert audio.shape == (2,)
    assert audio.tolist() == pytest.approx([0.0, 0.5])


def test_decode_8bit_unsigned():
    audio, _ = decode(make_wav(bytes([128, 255, 0]), width=1))
    assert audio.dtype == np.float32
    assert audio.tolist() == pytest.approx([0.0, 127 / 128, -1.0])


def test_decode_24bit_raises():
    with pytest.raises(ValueError, match="24-bit"):
        decode(make_wav(b"\x00\x00\x00" * 4, width=3))


@pytest.mark.parametrize("junk", [b"not a wav at all", b""])
def test_decode_junk_raises(junk):
    with pytest.raises(ValueError, match="not a readable WAV"):
        decode(junk)


def test_decode_zero_frames_raises():
    with pytest.raises(ValueError, match="no audio"):
        decode(make_wav(b""))


@pytest.mark.parametrize("rate", [0, 1, 7999, 48001])
def test_decode_rate_out_of_bounds_raises(rate):
    wav = bytearray(make_wav(pcm16(1, 2, 3)))
    wav[24:28] = struct.pack("<I", rate)
    with pytest.raises(ValueError, match="sample rate"):
        decode(bytes(wav))


@pytest.mark.parametrize("rate", [8000, 48000])
def test_decode_rate_bounds_inclusive(rate):
    assert decode(make_wav(pcm16(1, 2, 3), rate=rate))[1] == rate


def test_decode_too_many_channels_raises():
    with pytest.raises(ValueError, match="channels"):
        decode(make_wav(pcm16(*range(6)), channels=3))


def test_decode_truncates_to_cap(caplog):
    wav = make_wav(pcm16(*([100] * 8000)) * 200)
    with caplog.at_level("WARNING", logger="zeroout.hear"):
        audio, rate = decode(wav)
    assert len(audio) == rate * hear.MAX_AUDIO_SECONDS == 8000 * 180
    assert len(caplog.records) == 1


def test_decode_at_cap_not_truncated(caplog):
    wav = make_wav(pcm16(*([100] * 8000)) * 180)
    with caplog.at_level("WARNING", logger="zeroout.hear"):
        audio, _ = decode(wav)
    assert len(audio) == 8000 * 180 and not caplog.records


def test_words_leading_space_starts_word():
    words = words_from_tokens([" Th", "ank", " you"], [0.0, 0.25, 0.5], [0.25, 0.25, 0.5])
    assert words == [Word("Thank", 0.0, 0.5), Word("you", 0.5, 1.0)]


def test_words_bare_space_token_takes_next_text():
    words = words_from_tokens([" ", "1"], [1.0, 1.16], [0.16, 0.24])
    assert words == [Word("1", 1.0, 1.4)]


def test_words_punctuation_joins_text_without_moving_end():
    words = words_from_tokens([" ess", "."], [2.0, 2.56], [0.4, 0.32])
    assert words == [Word("ess.", 2.0, 2.4)]


def test_words_real_sequence():
    tokens = [" pr", "ess", " ", "1", "."]
    starts = [7.60, 7.84, 8.08, 8.24, 8.72]
    durations = [0.24, 0.24, 0.16, 0.24, 0.24]
    assert words_from_tokens(tokens, starts, durations) == [
        Word("press", 7.6, 8.08),
        Word("1.", 8.08, 8.48),
    ]


def test_words_punctuation_before_any_word_dropped():
    assert words_from_tokens(["?", " ok"], [0.0, 0.5], [0.2, 0.2]) == [Word("ok", 0.5, 0.7)]


def test_words_empty_words_dropped():
    assert words_from_tokens([" ", " ", " hi"], [0.0, 0.1, 0.2], [0.1, 0.1, 0.1]) == [Word("hi", 0.2, 0.3)]


def test_words_times_rounded():
    assert words_from_tokens([" a"], [0.123456], [0.2]) == [Word("a", 0.12, 0.32)]


def test_words_empty_input():
    assert words_from_tokens([], [], []) == []


class FakeResult:
    tokens = [" press", " ", "1", "."]
    timestamps = [1.0, 1.5, 1.6, 2.0]
    durations = [0.5, 0.1, 0.3, 0.2]


class FakeStream:
    def __init__(self):
        self.accepted = None
        self.result = FakeResult()

    def accept_waveform(self, rate, samples):
        self.accepted = (rate, samples)


class FakeRecognizer:
    def __init__(self):
        self.stream = FakeStream()
        self.decoded = False

    def create_stream(self):
        return self.stream

    def decode_stream(self, stream):
        assert stream is self.stream
        self.decoded = True


def test_transcribe_with_fake_recognizer():
    rec = FakeRecognizer()
    words = transcribe(make_wav(pcm16(0, 1000, -1000)), "unused", recognizer=rec)
    rate, samples = rec.stream.accepted
    assert rate == 8000 and samples.dtype == np.float32 and samples.ndim == 1 and len(samples) == 3
    assert rec.decoded
    assert words == [Word("press", 1.0, 1.5), Word("1.", 1.5, 1.9)]


def test_transcribe_bad_wav_raises_and_skips_recognizer():
    rec = FakeRecognizer()
    with pytest.raises(ValueError):
        transcribe(b"junk", "unused", recognizer=rec)
    assert rec.stream.accepted is None


def test_transcribe_no_words_is_empty():
    rec = FakeRecognizer()
    rec.stream.result = type("R", (), {"tokens": [], "timestamps": [], "durations": []})()
    assert transcribe(make_wav(pcm16(1, 2)), "unused", recognizer=rec) == []


def test_durations_mismatch_raises():
    rec = FakeRecognizer()
    rec.stream.result = type("R", (), {"tokens": [" a"], "timestamps": [0.0], "durations": []})()
    with pytest.raises(RuntimeError, match="Parakeet TDT"):
        transcribe(make_wav(pcm16(1, 2)), "unused", recognizer=rec)


NAME = "fake-parakeet"
FILES = {
    "encoder.int8.onnx": b"enc",
    "decoder.int8.onnx": b"dec",
    "joiner.int8.onnx": b"join",
    "tokens.txt": b"a 0\n",
}


@pytest.fixture
def model(tmp_path, monkeypatch):
    folder = tmp_path / NAME
    folder.mkdir()
    for name, data in FILES.items():
        (folder / name).write_bytes(data)
    pinned = {name: hashlib.sha256(data).hexdigest() for name, data in FILES.items()}
    monkeypatch.setitem(hear.MODELS, NAME, pinned)
    monkeypatch.setattr(hear, "_cache", {})
    return folder


def test_verify_returns_paths(model):
    enc, dec, join, tok = hear._verify(str(model))
    assert enc.endswith("encoder.int8.onnx") and dec.endswith("decoder.int8.onnx")
    assert join.endswith("joiner.int8.onnx") and tok.endswith("tokens.txt")


def test_verify_trailing_slash_ok(model):
    assert len(hear._verify(str(model) + "/")) == 4


def test_unknown_model_name_raises(tmp_path):
    (tmp_path / "other").mkdir()
    with pytest.raises(RuntimeError) as err:
        hear._verify(str(tmp_path / "other"))
    assert "other" in str(err.value) and "sherpa-onnx-nemo-parakeet-tdt-0.6b-v3-int8" in str(err.value)


def test_missing_model_dir_raises(tmp_path):
    missing = str(tmp_path / "sherpa-onnx-nemo-parakeet-tdt-0.6b-v3-int8")
    with pytest.raises(RuntimeError) as err:
        transcribe(make_wav(pcm16(1, 2)), missing)
    assert missing in str(err.value) and "ASR_MODEL_DIR" in str(err.value)


def test_missing_file_raises(model):
    (model / "joiner.int8.onnx").unlink()
    with pytest.raises(RuntimeError, match="ASR_MODEL_DIR"):
        hear.load(str(model))


def test_hash_mismatch_names_file(model):
    (model / "tokens.txt").write_bytes(b"tampered")
    with pytest.raises(RuntimeError, match="tokens.txt.*pinned hash"):
        hear.load(str(model))


def test_load_builds_once_per_dir(model, monkeypatch):
    built: list[tuple] = []
    monkeypatch.setattr(hear, "_build", lambda paths: built.append(paths) or FakeRecognizer())
    first = hear.load(str(model))
    assert hear.load(str(model)) is first
    assert len(built) == 1


def test_load_failure_not_cached(model, monkeypatch):
    (model / "tokens.txt").write_bytes(b"tampered")
    with pytest.raises(RuntimeError):
        hear.load(str(model))
    (model / "tokens.txt").write_bytes(FILES["tokens.txt"])
    monkeypatch.setattr(hear, "_build", lambda paths: FakeRecognizer())
    assert isinstance(hear.load(str(model)), FakeRecognizer)


def test_transcribe_uses_loaded_recognizer(model, monkeypatch):
    rec = FakeRecognizer()
    monkeypatch.setattr(hear, "_build", lambda paths: rec)
    words = transcribe(make_wav(pcm16(1, 2, 3)), str(model))
    assert words == [Word("press", 1.0, 1.5), Word("1.", 1.5, 1.9)]
    assert rec.decoded
