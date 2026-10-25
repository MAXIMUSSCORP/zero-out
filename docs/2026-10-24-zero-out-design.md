# Zero-Out: Design Spec

**Date:** 2026-10-24
**Event:** NVIDIA x Nebius Global AI Hackathon, Best Apps & Agents track (submission due Fri Oct 30, 10:00 am PT; target Thu Oct 29)
**Team:** solo
**Stack:** Python (FastAPI, Jinja + HTMX), SQLite, Twilio, Nebius Token Factory

## 1. Purpose

Zero-Out is a public, community-maintained map of company phone menus (IVRs), the kind of open data GitHub hosts for code. Every time someone uses it, the map gets more complete and more current.

- **Users:** anyone stuck in a US toll-free phone menu (airlines, banks, telecoms, utilities, IRS).
- **Value:** see the whole menu tree, ask "which buttons for X?", or press **Connect me** and get a phone call when the company's hold queue is reached.
- **Community loop:** crawls and every Connect me call feed observations back into the shared tree, which shows confirmations, staleness and a change timeline.

**Success at judging:** a judge opens the live site, browses real mapped menus for about 15 well-known US companies, asks the Q&A box for a path, and sees the eval numbers (§7) in the README and video.

**Hackathon compliance:** every model call is a runtime call to Nebius Token Factory using NVIDIA open models (Nemotron 3 Nano Omni, Nano and Super). The demo stays live until Dec 15.

> **Changed 2026-10-25.** Nemotron 3 Nano Omni is not served on Token Factory and no Token Factory model takes audio. Speech is recognized locally by NVIDIA Parakeet, and Nemotron 3 Nano reads the timed transcript. See `2026-10-25-local-app-plan.md`.

## 2. Approach

**Path replay, one call per menu node.** Each call dials, replays a known key path with measured waits between keys, then records what plays next. Audio is processed after the call; there is no live streaming. Crawling, path verification and Connect me all use this one building block.

Rejected:
- **Live in-call streaming agent:** too much real-time risk for a solo build, and phone menus rarely offer a reliable "back" key.
- **Browser calling with live hints:** the heaviest to build, and it would still need the live-streaming pieces.

## 3. Components

| Unit | Responsibility | Depends on |
|---|---|---|
| `probe.py` | `probe(number, path) -> recording`. Places one Twilio call; the path is a list of `(key, wait_seconds)`; plays the keys after each wait, then records about 40 s. | Twilio |
| `listen.py` | `listen(audio) -> Observation`. Sends the audio to **Nemotron 3 Nano Omni** on Token Factory and returns validated JSON: `prompt_text`, `options[{key,label}]`, `outcome` ∈ {`menu`,`hold`,`human`,`voicemail`,`hangup`,`invalid_input`,`needs_input`,`speech_menu`}, `prompt_seconds`. Rejects keys outside `0-9*#`. | Token Factory |
| `tree.py` | Pure merge logic over nodes and observations (§4). Its only model call: **Nemotron 3 Nano** decides whether a new prompt means the same as the stored one. | DB, Token Factory |
| `crawl.py` | Breadth-first exploration of `unexplored` nodes, shallowest first. Stops at `hold`/`human`/`needs_input`, at max depth 4, or at the budget (40 calls and 30 min per company per day). | probe, listen, tree |
| `connect.py` | Puts the company call into a Twilio Conference and replays the path. After the last key it dials the verified user's phone into the conference. The company side is recorded **only until the user joins**, then cut into one segment per menu step and merged. | probe, listen, tree |
| `ask.py` | **Nemotron 3 Super** gets the company tree as JSON and returns `{path, explanation, last_confirmed}`. Plain code checks that the path exists. Model ID is configurable (Ultra is a one-line swap). | Token Factory |
| `web/` | Pages: company list, collapsible tree (confirmations and last-confirmed badge per node, a play button for the prompt's audio), per-node history, company change timeline, Q&A box, "Map this number" (queued), "Connect me" (text-message verified). API: `GET /api/companies/{id}/tree`. | all |

**Database (SQLite):**
- `companies(id, name, number)`
- `nodes(company_id, path, prompt_text, options_json, outcome, prompt_seconds, status, confirmations, first_seen, last_confirmed)`. Status ∈ {`unexplored`,`mapped`,`changed`,`stale`,`human`,`needs_input`,`speech_menu`}.
- `observations(id, company_id, path, run_id, raw_json, audio_url, created_at)`
- `runs(id, company_id, kind ∈ {crawl, connect}, contributor_handle, calls, minutes, cost_usd, started_at, ended_at)`

Out of scope for v1: live streaming, user accounts beyond the text-message phone check, mirroring trees into a GitHub repo, comments and votes, speaking answers to speech-only menus, non-US numbers.

## 4. Data flow and merge rules

**Building a path:** the wait before each key = the parent node's `prompt_seconds` + 1.5 s margin, plus that node's timing correction (below).

**Crawl:**
1. Probe the empty path to record the greeting, merge it, and create an `unexplored` child per option.
2. Take the shallowest `unexplored` node, build its path, then probe, listen and merge.
3. Repeat until nothing is left or the budget is hit. Record calls, minutes and cost on the `run`.

**Merge (`tree.py`):**
- `unexplored` + valid observation → `mapped`; store the fields; create `unexplored` children for each option.
- `mapped` + same option keys + Nano says "same meaning" → `confirmations += 1`, update `last_confirmed`.
- `mapped` + different → `changed`; the old version stays in `observations`; the new observation becomes current; all descendants become `stale` and are queued for a budgeted re-crawl.
- outcome `invalid_input` (menu repeated or "invalid selection") → this node's timing correction += 2 s, retry once.
- outcome `human` during a crawl → status `human`, never crawled again; only Connect me re-checks it.
- `needs_input` / `speech_menu` → stored as a terminal state with the options heard; the crawler does not go deeper.
- `unparsed` observation → nothing is written to `nodes`.

**Connect me:** we choose when each key is pressed, so we know the timestamps. The company-side recording, which stops at user join, is cut at those times, and each segment is merged as an observation for its node. One real use re-checks the whole path.

## 5. Error handling, safety and limits

- **Telephony:** busy, no-answer or failed calls are retried 2× with backoff, then logged on the run; the node stays `unexplored`.
- **Model output:** invalid JSON gets one retry with a stricter prompt, then `unparsed`. Nothing is ever guessed into the tree.
- **Rate limits:** on 429, honor `Retry-After` with exponential backoff. Crawls are sequential, one call at a time.
- **Model availability:** model IDs live in config. **Day-1 blocker check:** confirm Nano Omni accepts audio on Token Factory (`GET /v1/models` and a test clip). If it doesn't, fall back to an NVIDIA speech-recognition model hosted on Token Factory, if any, and reassess.
- **Personal data:** the crawler never enters account numbers, dates of birth or PINs, real or fake.
- **Being a polite caller:** every crawl call ends at most 40 s after its last key press (audio is analyzed after the call, so there's no instant hang-up), and nodes marked `human` are never crawled again; at most 1 concurrent call per company; at most 40 crawl calls per company per day; real caller ID; only US toll-free numbers (800/888/877/866/855/844/833).
- **Spend:** a global daily $ cap in config, checked before every call. When it's hit, the site says so.
- **Connect me** only dials a phone number verified by text-message code (Twilio Verify).
- **Privacy:** no recording after the user joins a Connect me call.
- **Q&A honesty:** the path must exist in the tree, otherwise "I'm not sure" plus the top-level options. Every answer shows `last_confirmed`, with a warning when the path is `stale`.

## 6. Testing

pytest only:
- `test_tree.py`: each merge rule in §4.
- `test_path.py`: wait times computed from `prompt_seconds`, the margin and the correction.
- `test_segment.py`: cutting Connect me recordings at known key-press timestamps.
- `test_ask.py`: the path check rejects paths not in the tree.
- **Fake phone menu:** a local stand-in for Twilio that serves pre-recorded clips keyed by path, so the crawler runs end to end offline at no cost.

## 7. Evaluation (headline numbers for README and video)

- **Listen accuracy:** about 20 hand-labelled real prompts; measure key-to-label accuracy and outcome accuracy.
- **Coverage:** nodes mapped per company, and $ and minutes per crawl, for about 15 US toll-free lines.
- **Accuracy:** the share of mapped paths that a later replay confirmed reach the stated department.
- **Time saved:** seconds to reach the hold queue using Zero-Out's path vs listening manually, across several companies.
- **Freshness:** at least one real "menu changed" event captured in a timeline.

**Video hook (first 30 s):** a question → the path `2 → 4 → 0`, confirmed 3 h ago → one tap on Connect me → the phone rings with hold music already playing → cut to the tree and its change timeline.

## 8. Risks

| Risk | Mitigation |
|---|---|
| Nano Omni audio input unavailable or weak on Token Factory | Day-1 check; speech-recognition fallback; listen-accuracy eval exposes weakness early |
| Prompt timing (pressing too early) | measured `prompt_seconds` + margin + per-node correction knob |
| Twilio trial accounts can only call verified numbers | upgrade the account (~$20) on Day 1 |
| Call costs during crawls | per-company and global caps; toll-free only |
| Companies' terms of service and goodwill | polite-caller rules (§5), hang up on humans |
| Menus that need an account number or speech | marked as terminal states, visible on the site |
