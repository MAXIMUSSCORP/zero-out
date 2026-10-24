FROM python:3.11-slim

WORKDIR /app
COPY pyproject.toml README.md LICENSE ./
COPY zeroout ./zeroout
RUN pip install --no-cache-dir .

# SQLite DB and audio live here; mount a persistent volume.
VOLUME /app/data
EXPOSE 8000
CMD ["uvicorn", "zeroout.web:app", "--host", "0.0.0.0", "--port", "8000"]
