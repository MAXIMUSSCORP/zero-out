# Zero-Out

A public, community-maintained map of US toll-free phone menus (IVRs). Browse a company's whole menu tree, ask "which buttons for X?", or press **Connect me** and get a call back once the company's hold queue is reached. Every crawl and every Connect me call feeds observations back into the shared tree, which shows confirmations, staleness and a change timeline.

Built for the NVIDIA x Nebius Global AI Hackathon (Best Apps & Agents). Every model is an NVIDIA open model. Nemotron runs on **Nebius Token Factory**; speech recognition runs locally with NVIDIA **Parakeet**, because no Token Factory model takes audio.

## Quick start

Zero-Out runs on your own computer. It needs Python 3.11.4 or newer. Install it with one of these:

```bash
pipx install git+https://github.com/<owner>/zero-out    # or: uv tool install git+https://github.com/<owner>/zero-out
zeroout setup
zeroout run
```

`zeroout run` serves on http://127.0.0.1:8000 only and opens the browser with a private link for this launch. Other websites and other users on the computer cannot use it without that link. Use `--port` or `--no-browser` (it then prints the link) to change that.

- `zeroout setup` asks for a Nebius Token Factory API key and checks it live. Re-running it keeps the existing values, so press Enter to keep one.
- Settings go in a per-user file that only you can read: `~/.config/zeroout/zeroout.env` on Linux, `~/Library/Application Support/zeroout/zeroout.env` on macOS, and `%APPDATA%\zeroout\zeroout.env` on Windows.
- Data (the database, recordings and the speech model) goes in `~/.local/share/zeroout` on Linux, the same Application Support folder on macOS, and `%LOCALAPPDATA%\zeroout` on Windows. Set `ZEROOUT_HOME` to keep everything in one folder instead.
- `zeroout setup` downloads the speech model, and you choose one of two. Parakeet v3 is about 470 MB, covers 25 languages including Spanish, and is recommended. Parakeet 110M is about 100 MB, English only, and less accurate. Both are checked against pinned SHA-256 hashes.
- Twilio is optional. Without it, you can map a menu with your own phone instead. See [Map a menu with your own phone](#map-a-menu-with-your-own-phone), and [Twilio and your own number](#twilio-and-your-own-number) for the calls that need it.
- The install from git uses the newest compatible dependencies. The Docker image installs from `requirements.lock` with every package hash checked.

## Map a menu with your own phone

This needs no Twilio account. It works in the local app only.

1. Run `zeroout run` and open a company page. Click **Map this menu with my phone**.
2. Call the company on your own phone, on speaker, with the computer next to you. When it answers, click **Start listening**.
3. Zero-Out records through the computer's microphone and stops by itself when the menu goes quiet. Your browser asks for microphone permission the first time.
4. It shows the options it heard, then tells you what to press next, such as "Press 1 now", or which keys to press after you call again for another branch.

The microphone audio is never saved. If a person or a voicemail answers, none of their words are kept.

## Share with the community

Sharing is not live yet. Once it is live, trees will be shared through the public repo [MAXIMUSSCORP/zero-out-trees](https://github.com/MAXIMUSSCORP/zero-out-trees), and a read-only website built from that repo will be at https://maximusscorp.github.io/zero-out-trees/.

**Share with the community** is on a company page, or run `zeroout contribute --company-id N`. It previews the exact file first. The file is redacted: digit runs, emails and numbers in names are replaced. It holds no audio, no costs, no phone number of yours, and only dates.

Sharing opens a public pull request under your GitHub account, through the `gh` CLI. The file is public before review, and GitHub keeps it. Without `gh`, the file goes to an outbox folder, with a link to upload it by hand.

**Update from community** is on the home page, or run `zeroout update`. It brings in trees only for companies you added yourself. Other numbers are listed as "Offered by the community". They are unverified, so check a number against your card, statement or the company's website before you add it. Community data fills gaps and never changes what you mapped yourself.

## Twilio and your own number

Twilio is optional. You need it for automatic crawls and for Connect me.

You don't buy a Twilio number. Setup verifies your own phone number as your caller ID: Twilio calls you, and you type the 6-digit code on the keypad. Connect me then calls you at that number. Twilio's Calls API accepts only a Twilio number or a verified outgoing caller ID as the From number, which is why your own verified number works.

Twilio bills per minute. A trial account can only call numbers you have verified, so the account must be upgraded before Zero-Out can call companies.

Without Twilio, you can still map menus with your own phone. See [Map a menu with your own phone](#map-a-menu-with-your-own-phone).

## How it works

Every call is a *path replay*: dial, wait for each prompt, press the next key, then record about 40 s. Audio is analysed after the call, not streamed. Crawling, verification and Connect me all use that one building block.

| Model | Job | Where it's called |
|---|---|---|
| Parakeet TDT 0.6B v3 (local, `ASR_MODEL_DIR`) | Turns a recorded prompt into words with start and end times | `zeroout/hear.py` → `transcribe()` |
| Nemotron 3 Nano (`MODEL_NANO`, Token Factory) | Reads the timed transcript and returns `{prompt_text, options[{key,label}], outcome, prompt_seconds}` | `zeroout/listen.py` → `listen()` |
| Nemotron 3 Nano (`MODEL_NANO`, Token Factory) | Decides whether a re-heard prompt means the same as the stored one (confirm vs. changed) | `zeroout/tree.py` → `nano_same_meaning()` |
| Nemotron 3 Super (`MODEL_SUPER`, Token Factory) | Answers "which buttons for X?" from the tree JSON; code then checks the path exists | `zeroout/ask.py` → `ask()` |

Nano and Super go through one OpenAI-compatible client (`zeroout/llm.py` → `chat_json()`), pointed at `TF_BASE_URL`, using JSON mode. Invalid JSON gets one stricter retry, and a 429 honours `Retry-After`.

**Merge rules** (`zeroout/tree.py`): a first observation maps a node and creates unexplored children. A same-meaning re-observation adds a confirmation. A different one marks the node `changed`, keeps the old observation, and marks its descendants `stale` for re-crawling. `invalid_input` adds 2 s to that node's timing correction and retries once. Nothing the model returns is guessed into the tree; unparseable output is dropped.

```
zeroout/
  paths.py       number normalisation (vanity letters → digits), toll-free check, key timing
  probe.py       Twilio call: pauses + DTMF, record, download
  hear.py        Parakeet speech recognition → timed words
  listen.py      timed transcript → Nano → Observation
  tree.py        merge rules, tree JSON
  crawl.py       breadth-first crawl within budget
  connect.py     Connect me: replay path in a conference, dial the verified user, stop recording on join
  phone.py       phone in hand: take a clip, merge it, say what to press next
  community.py   share and import community trees
  github.py      gh pull requests, community download
  site_build.py  the static community site
  ask.py         grounded Q&A
  web.py         FastAPI + Jinja + HTMX site and JSON API
eval/            listen accuracy and coverage reports
```

## Development

For working on the code from a checkout. Zero-Out never reads a `.env` from the current folder on its own; point it at the checkout's file with `ZEROOUT_ENV_FILE=.env`, for example `ZEROOUT_ENV_FILE=.env zeroout run` or `ZEROOUT_ENV_FILE=.env python -m zeroout.listen clip.wav`. Real environment variables come first, then that file, then your per-user settings.

```bash
python -m venv .venv && . .venv/bin/activate
pip install -e '.[dev]'
cp .env.example .env    # fill in Token Factory and Twilio credentials and the model IDs
python -c "import tarfile, urllib.request; p, _ = urllib.request.urlretrieve('https://github.com/k2-fsa/sherpa-onnx/releases/download/asr-models/sherpa-onnx-nemo-parakeet-tdt-0.6b-v3-int8.tar.bz2'); tarfile.open(p, 'r:bz2').extractall('models', filter='data')"   # Parakeet, ~470 MB download
pytest                  # runs fully offline against a fake phone menu
ZEROOUT_ENV_FILE=.env zeroout run
```

Crawl one company from the CLI (this places real calls and costs cents): `ZEROOUT_ENV_FILE=.env zeroout crawl --company-id 1 --max-calls 3`.

Smoke-test listening on a clip: `ZEROOUT_ENV_FILE=.env python -m zeroout.listen path/to/prompt.wav` prints the timed transcript Nano read, then the observation. `ZEROOUT_ENV_FILE=.env python -m zeroout.hear path/to/prompt.wav` prints the words alone.

Docker: `docker build -t zero-out . && docker run -p 8000:8000 --env-file .env -v zeroout-data:/app/data zero-out`.

Deploy notes:

- `SESSION_SECRET` must be at least 32 characters (the app refuses to start otherwise). Generate one with `python -c 'import secrets;print(secrets.token_urlsafe(48))'`.
- Session cookies are HTTPS-only. Set `COOKIE_SECURE=0` only for local http development.
- The container runs uvicorn with `--proxy-headers` so rate limits see the real client IP. Set `FORWARDED_ALLOW_IPS` (read by uvicorn from the environment) to your reverse proxy's IP address, never `*`; otherwise clients could spoof their IP.
- The proxy must pass the public `Host` header through unchanged (nginx: `proxy_set_header Host $host;`). Form posts are checked against it, so a rewritten Host makes every Connect me and verify request fail with 403.

## Evaluation

- `ZEROOUT_ENV_FILE=.env python -m eval.listen_eval` scores `listen` against hand-labelled prompts in `eval/labels.json`, reporting key-to-label accuracy and outcome accuracy.
- `ZEROOUT_ENV_FILE=.env python -m eval.report` prints, per company, mapped nodes, crawl cost, minutes and confirmation rate (the share of mapped nodes confirmed at least twice).
- `eval/time_saved.csv` logs seconds to reach a hold queue manually vs. with Connect me.

From a checkout, prefix `ZEROOUT_ENV_FILE=.env` so the scripts use the checkout's settings.

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
