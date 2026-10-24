# Zero-Out

A public, community-maintained map of US toll-free phone menus (IVRs). Browse a company's whole menu tree, ask "which buttons for X?", or press **Connect me** and get a call back once the company's hold queue is reached. Every crawl and every Connect me call feeds observations back into the shared tree, which shows confirmations, staleness and a change timeline.

Built for the NVIDIA x Nebius Global AI Hackathon (Best Apps & Agents). Every model call is a runtime call to **Nebius Token Factory** running **NVIDIA Nemotron** open models.

## How it works

Every call is a *path replay*: dial, wait for each prompt, press the next key, then record about 40 s. Audio is analysed after the call, not streamed. Crawling, verification and Connect me all use that one building block.

| Model (on Token Factory) | Job | Where it's called |
|---|---|---|
| Nemotron 3 Nano Omni (`MODEL_OMNI`) | Hears a recorded prompt and returns `{prompt_text, options[{key,label}], outcome, prompt_seconds}` | `zeroout/listen.py` → `listen()` |
| Nemotron 3 Nano (`MODEL_NANO`) | Decides whether a re-heard prompt means the same as the stored one (confirm vs. changed) | `zeroout/tree.py` → `nano_same_meaning()` |
| Nemotron 3 Super (`MODEL_SUPER`) | Answers "which buttons for X?" from the tree JSON; code then checks the path exists | `zeroout/ask.py` → `ask()` |

All three go through one OpenAI-compatible client (`zeroout/llm.py` → `chat_json()`), pointed at `TF_BASE_URL`, using JSON mode. Invalid JSON gets one stricter retry, and a 429 honours `Retry-After`.

**Merge rules** (`zeroout/tree.py`): a first observation maps a node and creates unexplored children. A same-meaning re-observation adds a confirmation. A different one marks the node `changed`, keeps the old observation, and marks its descendants `stale` for re-crawling. `invalid_input` adds 2 s to that node's timing correction and retries once. Nothing the model returns is guessed into the tree; unparseable output is dropped.

```
zeroout/
  paths.py    number normalisation (vanity letters → digits), toll-free check, key timing
  probe.py    Twilio call: pauses + DTMF, record, download
  listen.py   Omni audio → Observation
  tree.py     merge rules, tree JSON
  crawl.py    breadth-first crawl within budget
  connect.py  Connect me: replay path in a conference, dial the verified user, stop recording on join
  ask.py      grounded Q&A
  web.py      FastAPI + Jinja + HTMX site and JSON API
eval/         listen accuracy and coverage reports
```

## Setup

```bash
python -m venv .venv && . .venv/bin/activate
pip install -e '.[dev]'
cp .env.example .env    # fill in Token Factory and Twilio credentials and the model IDs
pytest                  # runs fully offline against a fake phone menu
uvicorn zeroout.web:app --reload
```

Crawl one company from the CLI (this places real calls and costs cents): `python -m zeroout.crawl --company-id 1 --max-calls 3`.

Smoke-test Omni on a clip: `python -m zeroout.listen path/to/prompt.wav`.

Docker: `docker build -t zero-out . && docker run -p 8000:8000 --env-file .env -v zeroout-data:/app/data zero-out`.

Deploy notes:

- `SESSION_SECRET` must be at least 32 characters (the app refuses to start otherwise). Generate one with `python -c 'import secrets;print(secrets.token_urlsafe(48))'`.
- Session cookies are HTTPS-only. Set `COOKIE_SECURE=0` only for local http development.
- The container runs uvicorn with `--proxy-headers` so rate limits see the real client IP. Set `FORWARDED_ALLOW_IPS` (read by uvicorn from the environment) to your reverse proxy's IP address, never `*`; otherwise clients could spoof their IP.

## Evaluation

- `python -m eval.listen_eval` scores `listen` against hand-labelled prompts in `eval/labels.json`, reporting key-to-label accuracy and outcome accuracy.
- `python -m eval.report` prints, per company, mapped nodes, crawl cost, minutes and confirmation rate (the share of mapped nodes confirmed at least twice).
- `eval/time_saved.csv` logs seconds to reach a hold queue manually vs. with Connect me.

Results: these get filled in after the live crawl of the seed companies.

## Being a polite caller

- Only US toll-free numbers (800, 888, 877, 866, 855, 844, 833).
- At most 40 crawl calls, 30 minutes and depth 4 per company per day, and one call at a time per company.
- A global daily spend cap (`DAILY_CAP_USD`) is checked before every call; the site says so when it's reached.
- The crawler never enters account numbers, dates of birth or PINs, real or fake. Menus that ask for them are stored as terminal `needs_input`.
- Connect me only dials a phone verified by text-message code, and stops recording the moment the user joins.

## Limitations

- **Humans during crawls:** audio is analysed after the call, so the crawler can't hang up the instant a person answers. Every crawl call ends at most 40 s after its last key press, and a node marked `human` is never crawled again (only Connect me re-checks it).
- **Speech-only menus** ("say billing") and **menus that need an account number** are stored as terminal states; the crawler doesn't go deeper.
- **Timing:** keys are pressed after the parent prompt's measured length plus a 1.5 s margin (`KEY_MARGIN`) plus a per-node correction. Twilio pauses have 0.5 s resolution, so waits round up.

## License

MIT, see [LICENSE](LICENSE).
