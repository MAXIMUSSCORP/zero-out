# Zero-Out as a local app, with a community tree repo

Decided with the user on 2026-10-25. This supersedes the hosted-site parts of
`2026-10-24-zero-out-design.md`; the merge rules, crawl budget and polite-caller
rules there still hold.

## Why

1. **Nemotron 3 Nano Omni is not on Token Factory.** `GET /v1/models` lists 30
   models and none takes audio; `Nemotron-3-Nano-Omni` returns `model_not_found`
   and `/v1/audio/transcriptions` is 404 (checked 2026-10-25). So speech is
   recognized locally by NVIDIA Parakeet, and Nano reads the transcript.
2. **People run Zero-Out themselves** and share what they map. Each install keeps
   its calls, recordings and costs; only observations go to the community.

## Shape

- A person installs the app, runs `zeroout setup`, then `zeroout run` (localhost UI).
- Two ways to map a menu:
  - **Phone in hand** (default, no Twilio): they call on their own phone on
    speaker; the app listens through the PC microphone, shows the options, and
    says which key to press next for unmapped branches.
  - **Twilio** (optional): automatic crawls and Connect me. Their own mobile is
    verified in Twilio as the caller ID, so no Twilio number is bought.
- **Commit to community** opens a pull request on a public GitHub repo of trees.
  **Update from community** imports it. A read-only static site is built from
  that repo.

## Spike results (2026-10-25)

`sherpa-onnx` 2.0.0 with `sherpa-onnx-nemo-parakeet-tdt-0.6b-v3-int8`, on a
synthetic 19.7 s menu resampled to 8 kHz and round-tripped through mu-law:

- Loads in 1.9 s, decodes 19.7 s of audio in 0.62 s on 4 threads, peak RSS 1148 MB.
- Every token has a start and a duration. `result.words` and `segment_*` are
  empty, so words are joined from tokens (a leading space starts a word).
- A pause is absorbed by the next punctuation token, so lines are split at
  `.` `?` `!`, and a line ends at its last non-punctuation token. "press 1"
  ended at 8.48 s against a true 8.54 s.
- English menu text was exact apart from the brand ("Acme" heard as "ACNE").
  The Spanish bundled clip at 8 kHz was exact.
- sherpa-onnx 2.0 has no text-to-speech frontend; test clips were made with 1.13.8.
- The model ships as `.tar.bz2`; some machines have no `bzip2`, so setup unpacks
  it with Python's `tarfile`.

## Phases

### 1. Listen with Parakeet, then Nano
- `zeroout/hear.py`: `transcribe(wav, model_dir) -> list[Word(text, start, end)]`.
  stdlib `wave`, stereo mixed to mono, 8 or 16 bit; model loaded once behind a
  lock (crawls and Connect me run on background threads). Bad WAV gives `[]`.
- `zeroout/listen.py`: same signature. Words become timed lines
  (`[6.00-8.48] For billing questions, press 1.`) plus the recording's length;
  a text-only Nano call returns the same JSON; `prompt_seconds` is snapped to
  the nearest real line end. No speech: Nano is told the length, long means
  `hold`, short means `hangup`.
- `config.py`: `model_omni` becomes `asr_model_dir`. `.env.example` and `.env` follow.
- `pyproject.toml`: `sherpa-onnx`, `numpy`. Dockerfile downloads the model.
- Tests run offline with a fake recognizer; nothing is skipped. The real model is
  checked by `python -m zeroout.listen clip.wav` and `eval/listen_eval.py`.

### 2. Setup and run
- `zeroout setup`: Token Factory key (checked live), optional Twilio SID, token
  and verified caller ID (checked live), Parakeet download (v3 default, English
  110M as the light option), `SESSION_SECRET`, writes `.env`.
- `zeroout run`: binds `127.0.0.1`, `COOKIE_SECURE=0`, opens the browser.
- `[project.scripts]` entry, so `pipx install` works.
- With a verified caller ID, Connect me drops the SMS code step.

### 3. Phone in hand
- Browser microphone capture (`getUserMedia`) in the local UI, uploaded to the
  local server per prompt; the same `listen` turns it into an observation.
- Guided flow: "press 2 now" for the next unexplored branch, then listen again.
- Stops and discards when the outcome is `human`: recording a person needs
  consent in some states.

### 4. Community format and bookkeeping
- One file per contribution: `trees/<number>/<handle>-<UTC timestamp>.json`,
  never edited, so pull requests never conflict.
- Contents: company name and number, observations
  `{path, prompt_text, options, outcome, prompt_seconds, heard_at}`. Never audio,
  costs, the user's number, or anything after a user joined a Connect me call.
- `zeroout.community.check` validates (toll-free, keys, outcomes, no runs of 6+
  digits in prompt text, size) in the app and in the repo's CI.
- Migration: `observations.uid` (content hash), `origin` (`local`/`community`),
  `contributed_at`.
- Import replays community observations, oldest first, through `tree.merge()`;
  identical prompt text skips the Nano call.

### 5. Commit to community and Update from community
- Commit: with `gh` logged in, fork, branch, add file, open PR. Without it, write
  to `outbox/` and show how to submit by hand.
- Update: download the repo tarball with `urllib` (no git needed), import.

### 6. Community repo
- `trees/`, schema doc, `CONTRIBUTING.md`, a validator Action on every PR.
- A Pages Action replays all files into an in-memory database (exact text match,
  no model in CI) and renders read-only tree pages. "Which buttons for X?" stays
  in the app.
- Created on GitHub only when the user says so; owner and name to be decided.

### 7. Docs and review
- README states that speech recognition runs locally and only Nano and Super
  are Token Factory calls.
- `ecc:code-reviewer` pass per phase, commits after the user confirms.

## Done

- Phase 1, 2026-10-25, commit 128dae2. 375 tests. Live: the 8 kHz test menu gives
  options 1, 3, 4 and 9 with prompt_seconds 19.04 in about 7 s; silence gives hold
  for a crawl tail and nothing for a Connect piece.

- Phase 2, 2026-10-25, commit d6b4839. 499 tests. Verified end to end: installed
  with `uv tool install`, `zeroout setup` from an empty folder (live key check,
  real model download and hash checks, 32 s), `zeroout run` served the app, and
  a stray `.env`, a rebinding Host, a missing or foreign Origin, no launch
  session and a bad token were all refused. The phase 1 carried items are done
  (per-user model folder, pinned download in setup, 110M hashes, 64 MB cap,
  hashed locks, digest-pinned image).

- Phase 3, 2026-10-25, commit 9763fd0. 634 Python tests and the node recorder
  tests. Verified in real headless Chrome (Windows, driven over DevTools from a
  fake microphone that starts mid-speech): the recorder stopped by itself about
  5 s after the menu, the page showed the exact words for review, Keep merged
  them with `source=phone` and no audio stored, and the next instruction was
  "Press 1 now (billing questions)".
  Design changes from review: a Keep or Discard step before anything is
  saved; text kept only for menus, with digit runs and emails redacted; phone
  clips never overwrite or delete crawl data and never change crawl timing.

## Carried into phase 4

- Recorder levels are fixed (sound above 0.01 RMS, quiet below 0.02). In a
  room at or above 0.02 RMS of steady noise a clip runs to the 90 s cap (still
  read correctly, only slower) and dead air reads as hold; between 0.01 and
  0.02 dead air stops after 5 s as "nothing heard". Tune from real rooms.
- Publish phone-sourced text only after a crawl confirms it or the person
  reviewed it (they now always review before Keep); nodes carry `source`.
- Twilio caller ID with no purchased number is documented by Twilio's Calls API
  ("a Twilio number or a Verified outgoing caller id"), but not yet tried on a
  real upgraded account; the first real crawl confirms it.
- No Docker build has been run; the locks were checked by installing them with
  `--require-hashes` into a fresh Python 3.11 venv.
- The git install (pipx/uv) resolves the newest compatible dependencies; only
  the image uses the hashed lock.

## Open

- Community repo owner and name.
- Twilio verified caller ID rules (trial accounts, outbound use) checked against
  Twilio's current docs before phase 2.
