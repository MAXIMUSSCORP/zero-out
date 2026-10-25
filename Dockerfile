FROM python:3.11-slim

WORKDIR /app

# Speech recognition model (NVIDIA Parakeet TDT 0.6B v3, int8), about 650 MB unpacked.
# The archive is checked against a pinned SHA-256 before it is unpacked; zeroout/hear.py
# checks each model file again when it loads.
ARG ASR_MODEL=sherpa-onnx-nemo-parakeet-tdt-0.6b-v3-int8
ARG ASR_MODEL_SHA256=5793d0fd397c5778d2cf2126994d58e9d56b1be7c04d13c7a15bb1b4eafb16bf
RUN python -c "import hashlib, sys, tarfile, urllib.request; \
name, want = sys.argv[1], sys.argv[2]; \
path = '/tmp/' + name + '.tar.bz2'; \
urllib.request.urlretrieve('https://github.com/k2-fsa/sherpa-onnx/releases/download/asr-models/' + name + '.tar.bz2', path); \
h = hashlib.sha256(); f = open(path, 'rb'); [h.update(b) for b in iter(lambda: f.read(1 << 20), b'')]; f.close(); \
sys.exit('model archive hash mismatch') if h.hexdigest() != want else None; \
tarfile.open(path, 'r:bz2').extractall('/app/models', filter='data')" "$ASR_MODEL" "$ASR_MODEL_SHA256" \
 && rm -f /tmp/$ASR_MODEL.tar.bz2 \
 && rm -rf /app/models/$ASR_MODEL/test_wavs
ENV ASR_MODEL_DIR=/app/models/${ASR_MODEL}

COPY pyproject.toml README.md LICENSE ./
COPY zeroout ./zeroout
RUN pip install --no-cache-dir . \
 && useradd --system --uid 10001 --home /app app \
 && mkdir -p /app/data && chown -R app /app/data

USER app

# SQLite DB and audio live here; mount a persistent volume.
VOLUME /app/data
EXPOSE 8000
CMD ["uvicorn", "zeroout.web:app", "--host", "0.0.0.0", "--port", "8000", "--proxy-headers"]
