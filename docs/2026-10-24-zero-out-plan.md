# Zero-Out Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** A public site that maps US toll-free phone menus. A crawler and a "Connect me" feature keep the maps fresh, and Nemotron models on Nebius Token Factory hear the prompts and answer "which buttons for X?".

**Architecture:** One FastAPI service over one SQLite file. Every call is a path replay: dial, wait, press keys, record. Audio is analyzed after the call by Nemotron 3 Nano Omni. Pure merge logic (`tree.py`) is the core. Telephony and LLMs are passed in as plain functions, so the crawler runs offline against a fake phone menu.

**Tech Stack:** Python 3.11+, FastAPI, Jinja2 + HTMX (from a CDN), SQLite (stdlib `sqlite3`), `twilio`, `openai` (Token Factory is OpenAI-compatible), stdlib `wave`, `pytest`.

**Spec:** `docs/superpowers/specs/2026-10-24-zero-out-design.md` (read it first). Setup steps live in `FIRST_STEPS.md`.

**Repo root:** a new folder `zero-out/`, created in Task 1. All paths below are relative to it. Copy the spec into `zero-out/docs/` in Task 1.

## Global Constraints

- Every model call goes to Nebius Token Factory (`TF_BASE_URL`), with model IDs taken from env: `MODEL_OMNI` (Nemotron 3 Nano Omni), `MODEL_NANO` (Nemotron 3 Nano), `MODEL_SUPER` (Nemotron 3 Super). Don't use OpenRouter, Hugging Face or other proxies.
- Only US toll-free numbers can be called: area codes 800, 888, 877, 866, 855, 844, 833.
- Crawl budget per company per day: at most 40 calls, at most 30 minutes, max depth 4. At most 1 concurrent call per company.
- Key wait = parent `prompt_seconds` + **1.5 s** margin + the node's `timing_correction`. On `invalid_input`, the correction goes up by **2 s** and the step is retried once.
- Record about **40 s** after the last key press.
- Telephony retries: **2** with backoff. LLM JSON retries: **1** with a stricter prompt. On 429, honor `Retry-After`.
- Global daily spend cap `DAILY_CAP_USD`, checked before every call.
- Never enter personal data into a menu. Never record after the user joins a Connect me call.
- **Humans during crawls (deviation from spec §5):** audio is analyzed after the call, so the crawler can't hang up the instant a human answers. Every crawl call ends at most 40 s after its last key press, and a node marked `human` is never crawled again. Record this limitation in the README.
- Valid keys: `0-9`, `*`, `#`. Outcomes: `menu`, `hold`, `human`, `voicemail`, `hangup`, `invalid_input`, `needs_input`, `speech_menu`.
- Node statuses: `unexplored`, `mapped`, `changed`, `stale`, `human`, `needs_input`, `speech_menu`.
- Path encoding: keys joined by `.`. The root path is `""`.
- License: MIT. Secrets live only in `.env`, never committed.

## Review Focus

1. **Phone numbers people type** ("1-800-FLOWERS", "(888) 555 1234", no `+1`). Normalize to E.164, convert vanity letters, and give a clear error for anything else → test in Task 2.
2. **A model returning a menu with zero options, duplicate keys, or a non-English label.** A menu with no options counts as `unparsed`. On duplicate keys, the first one wins. Labels are kept as heard → test in Task 3.
3. **Empty or 0-second audio** (call dropped, recording missing). Treat it as a call failure and retry; never merge it → test in Task 6.
4. **Two people clicking "Map this number" for the same company at once.** The second gets "already running", not a second set of calls → test in Task 9.
5. **Asking about a company with nothing mapped yet.** Answer "not mapped yet" without calling the model → test in Task 8.

---

### Task 1: Project skeleton, config, database

**Files:**
- Create: `pyproject.toml`, `.env.example`, `.gitignore` (`.env`, `data/`, `__pycache__/`), `LICENSE` (MIT), `zeroout/__init__.py`, `zeroout/config.py`, `zeroout/models.py`, `zeroout/db.py`, `docs/2026-10-24-zero-out-design.md` (a copy of the spec)
- Test: `tests/test_db.py`

**Interfaces:**
- Produces:
  - `Settings` dataclass with fields `tf_api_key, tf_base_url, model_omni, model_nano, model_super, twilio_sid, twilio_token, twilio_from, twilio_verify_sid, daily_cap_usd: float, rate_per_min: float = 0.014, db_path: str = "data/zeroout.db", audio_dir: str = "data/audio", session_secret`, and `load_settings() -> Settings`, which reads env vars of the same names in upper case.
  - In `models.py`: dataclasses `Option(key: str, label: str)`, `Observation(prompt_text: str, options: list[Option], outcome: str, prompt_seconds: float)`, `Step(key: str, wait: float)`.
  - In `db.py`: `connect(path: str) -> sqlite3.Connection` (sets `row_factory = sqlite3.Row` and creates the tables if missing), `add_company(conn, name: str, number: str) -> int`, `get_node(conn, company_id: int, path: str) -> sqlite3.Row | None`.
  - Tables are exactly as in spec §3. Add `nodes.timing_correction REAL DEFAULT 0` and `PRIMARY KEY(company_id, path)`. `observations.audio_url` holds a local file path under `audio_dir`.

- [ ] **Step 1:** Run `git init` in `zero-out/`. Create a public GitHub repo, `zero-out`, with the MIT license. `pyproject.toml` deps: `fastapi uvicorn[standard] jinja2 python-multipart itsdangerous twilio openai python-dotenv`; dev: `pytest httpx`.
- [ ] **Step 2: Write the failing test.** `test_connect_creates_tables` checks that `connect(":memory:")` produces the tables `companies`, `nodes`, `observations` and `runs`. `test_add_company_and_get_missing_node` checks that `add_company` returns `1` and `get_node(conn, 1, "")` returns `None`.
- [ ] **Step 3:** Run `pytest tests/test_db.py -v`. Expected: FAIL (import error).
- [ ] **Step 4:** Implement `config.py`, `models.py` and `db.py` with the interfaces above. Use `python-dotenv`'s `load_dotenv()` in `load_settings`.
- [ ] **Step 5:** Run `pytest -v`. Expected: PASS.
- [ ] **Step 6: Commit.** `git commit -m "feat: skeleton, config, sqlite schema"`

### Task 2: Phone numbers and path building

**Files:**
- Create: `zeroout/paths.py`
- Test: `tests/test_paths.py`

**Interfaces:**
- Consumes: `db.get_node`, `Step`
- Produces: `normalize_number(raw: str) -> str` (E.164, raises `ValueError` with a human-readable message), `is_toll_free(e164: str) -> bool`, `build_path(conn, company_id: int, path: str) -> list[Step]`, `press_times(steps: list[Step]) -> list[float]` (cumulative waits)

- [ ] **Step 1: Write the failing tests.**

```python
def test_normalize_vanity(): assert normalize_number("1-800-FLOWERS") == "+18003569377"
def test_normalize_formats(): assert normalize_number("(888) 555 1234") == "+18885551234"
def test_normalize_rejects_garbage():
    with pytest.raises(ValueError): normalize_number("12345")
def test_toll_free(): assert is_toll_free("+18005551234") and not is_toll_free("+12125551234")
def test_build_path_uses_parent_prompt_margin_and_correction():
    # root prompt 12s; node "2" prompt 8s; node "2.1" correction 2s
    assert build_path(conn, cid, "2.1") == [Step("2", 13.5), Step("1", 11.5)]
def test_build_path_root_is_empty(): assert build_path(conn, cid, "") == []
def test_press_times(): assert press_times([Step("2", 13.5), Step("1", 11.5)]) == [13.5, 25.0]
```

- [ ] **Step 2:** Run `pytest tests/test_paths.py -v`. Expected: FAIL.
- [ ] **Step 3:** Implement. Vanity letters map to the standard keypad (`ABC→2 … WXYZ→9`). Strip everything else except digits. Ten digits means prepend `1`. If the result isn't 11 digits starting with `1`, raise `ValueError("Enter a US number like 1-800-555-1234")`. In `build_path`, the wait for the step that enters node `p` = `prompt_seconds(parent(p)) + 1.5 + timing_correction(p)`, where a missing node counts as 0.
- [ ] **Step 4:** Run `pytest tests/test_paths.py -v`. Expected: PASS.
- [ ] **Step 5: Commit.** `git commit -m "feat: number normalization and path timing"`

### Task 3: LLM client and `listen`

**Files:**
- Create: `zeroout/llm.py`, `zeroout/listen.py`
- Test: `tests/test_listen.py`

**Interfaces:**
- Consumes: `Settings`, `Observation`, `Option`
- Produces:
  - `chat_json(settings, model: str, messages: list[dict]) -> dict | None`. It uses `openai.OpenAI(base_url=settings.tf_base_url, api_key=settings.tf_api_key)` with `response_format={"type": "json_object"}`. On a JSON parse failure it retries once with a system line "Return ONLY valid JSON.", then returns `None`. On a 429 it sleeps `Retry-After` (default 2 s, doubling, at most 3 tries).
  - `parse_observation(raw: dict) -> Observation | None`
  - `listen(settings, wav: bytes) -> Observation | None`. Sends the audio as an `input_audio` content part (base64 WAV, the same format you confirmed works) plus a prompt asking for `{prompt_text, options:[{key,label}], outcome, prompt_seconds}`, with the outcome list copied from Global Constraints.

- [ ] **Step 1: Write the failing tests** (pure, no network).

```python
def test_parse_valid():
    o = parse_observation({"prompt_text":"For billing press 1","options":[{"key":"1","label":"billing"}],"outcome":"menu","prompt_seconds":6.2})
    assert o.options == [Option("1","billing")] and o.outcome == "menu"
def test_parse_rejects_bad_key(): assert parse_observation({... "options":[{"key":"A","label":"x"}] ...}) is None
def test_parse_rejects_unknown_outcome(): assert parse_observation({... "outcome":"robot" ...}) is None
def test_menu_without_options_is_unparsed(): assert parse_observation({... "options":[], "outcome":"menu" ...}) is None
def test_duplicate_keys_first_wins():
    o = parse_observation({... "options":[{"key":"1","label":"billing"},{"key":"1","label":"sales"}] ...})
    assert o.options == [Option("1","billing")]
def test_non_english_label_kept(): assert parse_observation({... [{"key":"2","label":"español"}] ...}).options[0].label == "español"
```

- [ ] **Step 2:** Run `pytest tests/test_listen.py -v`. Expected: FAIL.
- [ ] **Step 3:** Implement `llm.py` and `listen.py` per the interfaces.
- [ ] **Step 4:** Run `pytest tests/test_listen.py -v`. Expected: PASS.
- [ ] **Step 5: Live smoke check.** `python -m zeroout.listen path/to/test.wav` prints the parsed Observation. Record any test phone prompt with your own phone as the input.
- [ ] **Step 6: Commit.** `git commit -m "feat: Token Factory client and Omni listen"`

### Task 4: Tree merge rules

**Files:**
- Create: `zeroout/tree.py`
- Test: `tests/test_tree.py`

**Interfaces:**
- Consumes: `db`, `Observation`
- Produces:
  - `merge(conn, company_id: int, path: str, obs: Observation, run_id: int, audio_path: str, same_meaning: Callable[[str, str], bool]) -> str`. Returns one of `"mapped" | "confirmed" | "changed" | "retry" | "terminal"`. Always inserts an `observations` row, except on `"retry"`.
  - `ensure_root(conn, company_id) -> None`, `unexplored(conn, company_id, max_depth: int) -> list[str]` (shallowest first, then by path), `tree_json(conn, company_id) -> dict` (nested: `{path, prompt_text, status, outcome, confirmations, last_confirmed, children: [...]}`).
  - `nano_same_meaning(settings) -> Callable[[str, str], bool]`. Asks `MODEL_NANO` for `{"same": bool}`; `None` counts as `False`.

- [ ] **Step 1: Write the failing tests.** Use an in-memory DB and `same = lambda a, b: True` or `False`.
  - `test_unexplored_to_mapped_creates_children`: merge a menu with keys 1 and 2 at `""` → returns `"mapped"`; nodes `"1"` and `"2"` exist with status `unexplored`.
  - `test_same_meaning_confirms`: a second merge with the same keys and `same → True` returns `"confirmed"` and `confirmations == 2`.
  - `test_change_marks_descendants_stale`: map `""`, then `"1"`, then merge `""` with `same → False` → returns `"changed"`; `""` status is `changed`; `"1"` status is `stale`; the old observation row is still present.
  - `test_invalid_input_bumps_correction`: an `invalid_input` observation at `"1"` returns `"retry"`, `timing_correction == 2.0`, and no observation row is added.
  - `test_human_and_needs_input_are_terminal`: a `human` outcome sets status `human` and creates no children; `needs_input` likewise.
  - `test_tree_json_nests`: `tree_json` returns the root with 2 children in key order.
- [ ] **Step 2:** Run `pytest tests/test_tree.py -v`. Expected: FAIL.
- [ ] **Step 3:** Implement per spec §4. `hold`, `voicemail` and `hangup` set status `mapped` with no children. Descendants keep status `stale`, which shows on the site. `unexplored()` returns nodes whose status is `unexplored` **or** `stale`, so the crawler re-visits them.
- [ ] **Step 4:** Run `pytest tests/test_tree.py -v`. Expected: PASS.
- [ ] **Step 5: Commit.** `git commit -m "feat: tree merge rules"`

### Task 5: Audio segmentation

**Files:**
- Create: `zeroout/segment.py`
- Test: `tests/test_segment.py`

**Interfaces:**
- Produces: `segment(wav: bytes, times: list[float]) -> list[bytes]` returns `len(times)+1` WAV clips split at the given seconds. `tail(wav: bytes, start: float) -> bytes` returns the clip from `start` to the end. `duration(wav: bytes) -> float`.

- [ ] **Step 1: Write the failing test.** Build a 10 s silent 8 kHz mono WAV with stdlib `wave`. `segment(w, [3, 7])` returns 3 clips with durations ≈ `[3, 4, 3]` (±0.01). `tail(w, 7)` has a duration ≈ 3. Empty input to `duration(b"")` returns `0.0`.
- [ ] **Step 2:** Run `pytest tests/test_segment.py -v`. Expected: FAIL.
- [ ] **Step 3:** Implement with `wave` (`setpos` by frame index). Clamp times to the clip length.
- [ ] **Step 4:** Run `pytest tests/test_segment.py -v`. Expected: PASS.
- [ ] **Step 5: Commit.** `git commit -m "feat: wav segmentation"`

### Task 6: Phones (fake and Twilio) and the crawler

**Files:**
- Create: `zeroout/probe.py`, `zeroout/budget.py`, `zeroout/crawl.py`, `tests/fakes.py`
- Test: `tests/test_crawl.py`

**Interfaces:**
- Consumes: `build_path`, `press_times`, `merge`, `unexplored`, `ensure_root`, `tail`, `duration`
- Produces:
  - `ProbeResult(wav: bytes, seconds: float)`. A phone is any callable `probe(number: str, steps: list[Step]) -> ProbeResult`.
  - `twilio_probe(settings) -> probe callable`. Uses inline TwiML: `<Pause length=w>` then `<Play digits=k>` for each step, then `<Pause length="40"/><Hangup/>`. The call is created with `record=True`. It polls `calls(sid).fetch()` every 3 s until the call is completed (2-minute timeout), then downloads the `.wav` of the call's first recording. Busy, failed or no-answer statuses raise `CallFailed`.
  - `can_spend(conn, settings) -> bool`, which compares the sum of today's `runs.cost_usd` with `daily_cap_usd`.
  - `Budget(max_calls=40, max_minutes=30, max_depth=4)`.
  - `crawl(conn, settings, company_id, probe, listen, same_meaning, budget=Budget(), contributor="crawler") -> int` returns the `run_id`. Each loop: check `can_spend`, then `build_path`, `probe` (retry `CallFailed` 2× with 2 s and 4 s backoff), `tail(wav, sum of waits)`, `listen`, save the audio to `audio_dir`, then `merge`. On `"retry"`, re-probe once. Empty or 0 s audio counts as `CallFailed`. Updates `runs.calls`, `minutes` and `cost_usd = minutes * rate_per_min`.
  - In `tests/fakes.py`: `FakeMenu(tree: dict[str, Observation])` gives `.probe` (returns a 1 s silent WAV whose tail encodes the reached path) and `.listen` (decodes the path and returns `tree[path]`).

- [ ] **Step 1: Write the failing tests.** Fake menu: `""` = menu {1: billing, 2: agent}, `"1"` = hold, `"2"` = human.
  - `test_crawl_maps_whole_fake_menu`: statuses are `""` mapped, `"1"` mapped (outcome hold), `"2"` human; `runs.calls == 3`.
  - `test_budget_stops_after_one_call`: with `Budget(max_calls=1)`, only the root is mapped and its children stay `unexplored`.
  - `test_spend_cap_blocks_calls`: with `daily_cap_usd=0`, the crawl makes 0 calls.
  - `test_empty_audio_is_retried_not_merged`: a probe that returns `b""` twice and then real audio still maps the root, with no `observations` row from the empty attempts.
- [ ] **Step 2:** Run `pytest tests/test_crawl.py -v`. Expected: FAIL.
- [ ] **Step 3:** Implement per the interfaces.
- [ ] **Step 4:** Run `pytest -v`. Expected: all pass.
- [ ] **Step 5: Live check (costs cents).** `python -m zeroout.crawl --company-id 1 --max-calls 3` against one seed company. Inspect the `nodes` table. If keys get pressed too early, adjust the margin knob in config rather than in code.
- [ ] **Step 6: Commit.** `git commit -m "feat: probe, budget, crawler"`

### Task 7: Connect me

**Files:**
- Create: `zeroout/connect.py`
- Test: `tests/test_connect.py`

**Interfaces:**
- Consumes: `build_path`, `press_times`, `segment`, `merge`, `can_spend`
- Produces:
  - `ingest_connect_recording(conn, settings, company_id, path, wav, times, run_id, listen, same_meaning) -> list[str]`. It segments at `times` and merges segment *i* as the observation for the *i*-th prefix of `path` (the root first). It returns the merge results.
  - `start_connect(conn, settings, company_id, path, user_e164) -> int` (Twilio only, not unit-tested):
    1. Create the company call with TwiML = pauses and digits, then `<Dial><Conference endConferenceOnExit="true">zo-{run_id}</Conference></Dial>`.
    2. Once the call is `in-progress`, start a recording with `calls(sid).recordings.create()`.
    3. After the last press time, dial `user_e164` into the same conference.
    4. When the user's call is `in-progress`, stop the recording with `recordings(rec_sid).update(status="stopped")`.
    5. Download the recording and call `ingest_connect_recording`.
    Run steps 2 to 5 in a background thread.

- [ ] **Step 1: Write the failing test.** `test_connect_recording_confirms_every_node_on_path`: map the fake menu, then ingest a 3-segment recording for path `"1"` → results are `["confirmed", "confirmed"]` and both nodes have `confirmations == 2`.
- [ ] **Step 2:** Run `pytest tests/test_connect.py -v`. Expected: FAIL.
- [ ] **Step 3:** Implement both functions.
- [ ] **Step 4:** Run `pytest tests/test_connect.py -v`. Expected: PASS.
- [ ] **Step 5: Live check.** Run a Connect me call to your own verified phone on one mapped path. Confirm your phone rings at the hold queue and the stored recording ends before you said anything.
- [ ] **Step 6: Commit.** `git commit -m "feat: connect me with ride-along verification"`

### Task 8: Ask

**Files:**
- Create: `zeroout/ask.py`
- Test: `tests/test_ask.py`

**Interfaces:**
- Consumes: `tree_json`, `get_node`, `chat_json`
- Produces: `Answer(path: str | None, explanation: str, last_confirmed: str | None, stale: bool, top_options: list[Option])` and `ask(conn, settings, company_id, question, chat=chat_json) -> Answer`. `MODEL_SUPER` gets `tree_json` and returns `{path, explanation}`. The path is accepted only if `get_node` finds it with a status other than `unexplored`.

- [ ] **Step 1: Write the failing tests** (inject `chat`).
  - `test_valid_path_returned`: `chat` returns `{"path": "1", ...}` → `Answer.path == "1"`, `stale is False`.
  - `test_invented_path_rejected`: `chat` returns `"9.9"` → `path is None` and `top_options` equals the root's options.
  - `test_stale_flagged`: a node with status `stale` → `stale is True`.
  - `test_unmapped_company_skips_model`: with no root node, `chat` is never called and the explanation is `"Not mapped yet"`.
- [ ] **Step 2:** Run `pytest tests/test_ask.py -v`. Expected: FAIL.
- [ ] **Step 3:** Implement.
- [ ] **Step 4:** Run `pytest tests/test_ask.py -v`. Expected: PASS.
- [ ] **Step 5: Commit.** `git commit -m "feat: grounded Q&A over the tree"`

### Task 9: Web app

**Files:**
- Create: `zeroout/web.py`, `zeroout/templates/base.html`, `index.html`, `company.html`, `_tree_node.html`, `_answer.html`
- Test: `tests/test_web.py`

**Interfaces:**
- Consumes: everything above
- Produces: `create_app(settings, conn=None, probe=None, listen=None, same=None) -> FastAPI` (tests inject fakes). Routes:
  - `GET /` lists companies with mapped node counts.
  - `GET /c/{id}` shows the tree (badges for confirmations, last confirmed and status; an `<audio>` element per node pointing at `/audio/{obs_id}`) and a timeline of `changed` observations, newest first.
  - `POST /c/{id}/ask` returns the `_answer.html` partial.
  - `POST /companies` (form: name, number) normalizes the number and rejects anything that isn't toll-free with a 400 and the `ValueError` message.
  - `POST /c/{id}/crawl` runs `crawl` as a `BackgroundTask`. It keeps a per-company set of running crawls and returns `409 "Already mapping this company"` if one is already running.
  - `POST /verify/start` and `POST /verify/check` use Twilio Verify by text message; on success, the E.164 number goes into `request.session["phone"]`.
  - `POST /c/{id}/connect` (form: path) returns 403 without a verified phone in the session.
  - `GET /api/companies/{id}/tree` returns `tree_json`.
  - `GET /audio/{obs_id}` serves the file.
  - A simple in-memory limit per IP on `/ask` (20/hour) and `/connect` (3/hour).

- [ ] **Step 1: Write the failing tests** (FastAPI `TestClient`, fakes from `tests/fakes.py`).
  - `test_api_tree_is_nested_json`
  - `test_add_non_toll_free_rejected` (400)
  - `test_connect_requires_verified_phone` (403)
  - `test_second_crawl_same_company_conflicts`: mark the company as running → `POST /c/1/crawl` returns 409.
  - `test_company_page_renders_tree`: the response contains the label "billing".
- [ ] **Step 2:** Run `pytest tests/test_web.py -v`. Expected: FAIL.
- [ ] **Step 3:** Implement. Use `SessionMiddleware(secret_key=settings.session_secret)`. Use HTMX for ask and the collapsible tree (`<details>` elements are fine; no JS framework).
- [ ] **Step 4:** Run `pytest -v`. Expected: all pass.
- [ ] **Step 5:** Run `uvicorn zeroout.web:app --reload`, add a seed company, and browse it.
- [ ] **Step 6: Commit.** `git commit -m "feat: public site, api, verify, connect"`

### Task 10: Seed, evaluate, deploy, submit

**Files:**
- Create: `seeds.csv` (name, number), `eval/labels.json`, `eval/listen_eval.py`, `eval/report.py`, `eval/time_saved.csv`, `Dockerfile`, `README.md`

**Interfaces:**
- Consumes: `listen`, the database
- Produces:
  - `python -m eval.listen_eval` prints `key_accuracy` and `outcome_accuracy` over `labels.json` (`[{file, options, outcome}]`).
  - `python -m eval.report` prints, per company: mapped nodes, crawl $, minutes, and the confirmation rate (the share of mapped nodes with `confirmations >= 2`).

- [ ] **Step 1:** Fill in `seeds.csv` with about 15 US toll-free lines (airlines, banks, telecoms, utilities, the IRS). Copy each number from the company's own official contact page.
- [ ] **Step 2:** Label about 20 recorded prompts. Run `listen_eval` and keep the output.
- [ ] **Step 3:** Crawl all the seed companies within the cap. Re-crawl after a day or more to collect confirmations and any changes. Run `eval.report`.
- [ ] **Step 4:** Time 5 or more "reach a human" attempts manually vs with Connect me and log them in `time_saved.csv`.
- [ ] **Step 5:** Write the `Dockerfile`: `python:3.11-slim`, install the package, `uvicorn zeroout.web:app --host 0.0.0.0 --port 8000`, with `data/` on a persistent volume. Deploy to a host with a persistent disk. Confirm the public URL works and will stay up until Dec 15.
- [ ] **Step 6:** Write the README:
  - what it is
  - a table of which Nemotron model does what, and where Token Factory is called
  - setup steps
  - the eval numbers
  - limitations (speech menus, menus that need an account number, timing)
  - the polite-caller rules
  - MIT license
- [ ] **Step 7:** Record the video (under 3 minutes, opening with the spec §7 hook; say "Nebius Token Factory" and "Nemotron" out loud), fill in the Devpost form and the feedback section, and submit by **Thu Oct 29**.
- [ ] **Step 8: Commit.** `git commit -m "docs: eval, deploy, readme"`
