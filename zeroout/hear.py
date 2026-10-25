import hashlib
import io
import logging
import os
import shutil
import sys
import tarfile
import tempfile
import threading
import urllib.request
import wave
from pathlib import Path
from typing import Any, Callable

import numpy as np

from .models import Word

log = logging.getLogger(__name__)

MIN_RATE, MAX_RATE = 8000, 48000
MAX_AUDIO_SECONDS = 180

# Only these exports are accepted, byte for byte; the folder name picks the entry.
MODELS = {
    "sherpa-onnx-nemo-parakeet-tdt-0.6b-v3-int8": {
        "encoder.int8.onnx": "acfc2b4456377e15d04f0243af540b7fe7c992f8d898d751cf134c3a55fd2247",
        "decoder.int8.onnx": "179e50c43d1a9de79c8a24149a2f9bac6eb5981823f2a2ed88d655b24248db4e",
        "joiner.int8.onnx": "3164c13fc2821009440d20fcb5fdc78bff28b4db2f8d0f0b329101719c0948b3",
        "tokens.txt": "d58544679ea4bc6ac563d1f545eb7d474bd6cfa467f0a6e2c1dc1c7d37e3c35d",
    },
    "sherpa-onnx-nemo-parakeet_tdt_transducer_110m-en-36000-int8": {
        "encoder.int8.onnx": "0f35509ddeb9b39002fb077d979a9fe74f06eb0bc4dd5c34f512f82e5111d657",
        "decoder.int8.onnx": "f7c331c5504c2e593c76ed22b728e3f554af6c4a383dde862e719ced08b1da19",
        "joiner.int8.onnx": "bf7dff69e9f2cdbe9943d70da358f38b361c115ba0105bae7e908e0d6ec782f6",
        "tokens.txt": "450e56bd2f036fe5b6aa821865838cc5aa9d8b0106134ce9a9ba0664abe6cd10",
    },
}

# (sha256, bytes) of each release archive, GitHub's own asset digests.
ARCHIVES = {
    "sherpa-onnx-nemo-parakeet-tdt-0.6b-v3-int8": (
        "5793d0fd397c5778d2cf2126994d58e9d56b1be7c04d13c7a15bb1b4eafb16bf", 487170055,
    ),
    "sherpa-onnx-nemo-parakeet_tdt_transducer_110m-en-36000-int8": (
        "f628312e9fdf8686374cb01a69425c41732529d540860311f16f37cbc32cfe9b", 108035095,
    ),
}
RELEASE_URL = "https://github.com/k2-fsa/sherpa-onnx/releases/download/asr-models/{}.tar.bz2"

# One recognizer per model dir, shared by every thread, so build and decode both hold the lock.
_lock = threading.Lock()
_cache: dict[str, Any] = {}


def decode(wav: bytes) -> tuple[np.ndarray, int]:
    try:
        src = wave.open(io.BytesIO(wav), "rb")
    except (wave.Error, EOFError, ValueError) as e:
        raise ValueError("Recording is not a readable WAV.") from e
    with src:
        channels, width, rate, total = src.getnchannels(), src.getsampwidth(), src.getframerate(), src.getnframes()
        why = (
            f"{width * 8}-bit samples are not supported" if width not in (1, 2)
            else f"sample rate {rate} Hz is outside {MIN_RATE} to {MAX_RATE}" if not MIN_RATE <= rate <= MAX_RATE
            else f"{channels} channels, expected 1 or 2" if channels not in (1, 2)
            else "it has no audio" if not total
            else ""
        )
        if why:
            raise ValueError(f"Recording is not a readable WAV: {why}.")
        cap = rate * MAX_AUDIO_SECONDS
        if total > cap:
            log.warning("recording is %.0f s; keeping the first %d s", total / rate, MAX_AUDIO_SECONDS)
        try:
            raw = src.readframes(min(total, cap))
        except (wave.Error, EOFError) as e:
            raise ValueError("Recording is not a readable WAV.") from e
    if width == 2:
        samples = np.frombuffer(raw, dtype="<i2").astype(np.float32) / 32768.0
    else:
        samples = (np.frombuffer(raw, dtype=np.uint8).astype(np.float32) - 128.0) / 128.0
    usable = len(samples) - len(samples) % channels
    if not usable:
        raise ValueError("Recording is not a readable WAV: it has no audio.")
    mono = samples[:usable].reshape(-1, channels).mean(axis=1, dtype=np.float32)
    return mono.astype(np.float32), rate


def words_from_tokens(tokens: list[str], starts: list[float], durations: list[float]) -> list[Word]:
    words: list[Word] = []
    cur: list[Any] | None = None  # [text, start, end]

    def flush() -> None:
        if cur and cur[0]:
            words.append(Word(cur[0], round(cur[1], 2), round(cur[2], 2)))

    for tok, start, dur in zip(tokens, starts, durations):
        if tok == " ":
            flush()
            cur = ["", start, start]
        elif not any(c.isalnum() for c in tok):
            if cur and cur[0]:
                cur[0] += tok.strip()
        elif tok.startswith(" ") or cur is None:
            flush()
            cur = [tok.strip(), start, start + dur]
        else:
            cur[0] += tok
            cur[2] = start + dur
    flush()
    return words


def _sha256(path: str) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        while chunk := f.read(1 << 20):
            h.update(chunk)
    return h.hexdigest()


def _verify(model_dir: str) -> tuple[str, str, str, str]:
    name = os.path.basename(os.path.normpath(model_dir))
    pinned = MODELS.get(name)
    if pinned is None:
        raise RuntimeError(
            f"Unknown speech model folder {name!r}. Known models: {', '.join(MODELS)}. Set ASR_MODEL_DIR."
        )
    paths = {f: os.path.join(model_dir, f) for f in pinned}
    if not all(os.path.isfile(p) for p in paths.values()):
        raise RuntimeError(f"No Parakeet model in {model_dir}. Set ASR_MODEL_DIR to the folder of the downloaded model.")
    for f, p in paths.items():
        if _sha256(p) != pinned[f]:
            raise RuntimeError(f"{p} does not match the pinned hash; download the model again.")
    found = {f.split(".")[0].split("-")[0]: p for f, p in paths.items()}
    return found["encoder"], found["decoder"], found["joiner"], found["tokens"]


def _build(paths: tuple[str, str, str, str]) -> Any:
    import sherpa_onnx

    encoder, decoder, joiner, tokens = paths
    return sherpa_onnx.OfflineRecognizer.from_transducer(
        encoder=encoder, decoder=decoder, joiner=joiner, tokens=tokens,
        num_threads=min(4, os.cpu_count() or 1), model_type="nemo_transducer",
    )


def load(model_dir: str) -> Any:
    with _lock:
        if model_dir not in _cache:
            paths = _verify(model_dir)
            log.info("loading speech model from %s", os.path.abspath(os.path.dirname(paths[0])))
            _cache[model_dir] = _build(paths)
        return _cache[model_dir]


def _fetch(url: str, path: Path, progress: Callable[[int, int], None] | None, expected: int) -> None:
    limit = expected
    too_big = RuntimeError(f"The download is larger than expected ({expected} bytes); stopping.")
    with urllib.request.urlopen(url, timeout=60) as r, open(path, "wb") as out:
        total = int(r.headers.get("Content-Length") or expected)
        if total > limit:
            raise too_big
        done = 0
        while chunk := r.read(1 << 20):
            done += len(chunk)
            if done > limit:
                raise too_big
            out.write(chunk)
            if progress:
                progress(done, total)


def install(
    name: str, root: Path, *, fetch: Callable | None = None, progress: Callable[[int, int], None] | None = None
) -> Path:
    if name not in MODELS or name not in ARCHIVES:
        raise RuntimeError(f"Unknown speech model {name!r}. Known models: {', '.join(MODELS)}.")
    digest, size = ARCHIVES[name]
    root = Path(root)
    root.mkdir(parents=True, exist_ok=True)
    for stale in [*root.glob(".zeroout-download-*"), *root.glob(".zeroout-unpack-*")]:
        shutil.rmtree(stale, ignore_errors=True) if stale.is_dir() else stale.unlink(missing_ok=True)
    fd, tmp_name = tempfile.mkstemp(dir=root, prefix=".zeroout-download-", suffix=".tar.bz2")
    os.close(fd)
    tmp = Path(tmp_name)
    work = Path(tempfile.mkdtemp(dir=root, prefix=".zeroout-unpack-"))
    try:
        if fetch is not None:
            fetch(RELEASE_URL.format(name), tmp, progress)
        else:
            _fetch(RELEASE_URL.format(name), tmp, progress, size)
        if _sha256(str(tmp)) != digest:
            raise RuntimeError(f"The {name} download does not match the pinned hash; try again.")
        try:
            with tarfile.open(tmp, "r:bz2") as tar:
                tar.extractall(work, filter="data")
        except (tarfile.TarError, EOFError, OSError) as e:
            raise RuntimeError(f"Could not unpack the {name} download: {e}.") from e
        fresh = work / name
        if not fresh.is_dir():
            raise RuntimeError(f"The {name} download has no {name} folder inside.")
        shutil.rmtree(fresh / "test_wavs", ignore_errors=True)
        _verify(str(fresh))
        dest, old = root / name, work / "old"
        if dest.exists():
            os.replace(dest, old)
        try:
            os.replace(fresh, dest)
        except OSError:
            if old.exists():
                os.replace(old, dest)
            raise
    finally:
        tmp.unlink(missing_ok=True)
        shutil.rmtree(work, ignore_errors=True)
    return root / name


def _run(recognizer: Any, audio: np.ndarray, rate: int) -> list[Word]:
    stream = recognizer.create_stream()
    stream.accept_waveform(rate, audio)
    recognizer.decode_stream(stream)
    r = stream.result
    if len(r.durations) != len(r.tokens):
        raise RuntimeError("The speech model gave no word durations; Zero-Out needs a Parakeet TDT model.")
    return words_from_tokens(list(r.tokens), list(r.timestamps), list(r.durations))


def transcribe(wav: bytes, model_dir: str, *, recognizer: Any = None) -> list[Word]:
    audio, rate = decode(wav)
    if recognizer is not None:
        return _run(recognizer, audio, rate)
    recognizer = load(model_dir)
    with _lock:
        return _run(recognizer, audio, rate)


if __name__ == "__main__":
    if len(sys.argv) not in (2, 3):
        sys.exit("usage: python -m zeroout.hear file.wav [model_dir]")
    if len(sys.argv) == 3:
        model_dir = sys.argv[2]
    else:
        from .config import load_settings

        model_dir = load_settings().asr_model_dir
    with open(sys.argv[1], "rb") as f:
        for w in transcribe(f.read(), model_dir):
            print(f"{w.start:.2f} {w.end:.2f} {w.text}")
