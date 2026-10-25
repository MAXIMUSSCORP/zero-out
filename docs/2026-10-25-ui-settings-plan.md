# A friendlier Zero-Out: the redesign, Settings, and readable menus

Decided with the user on 2026-10-25. Three pieces of work:

1. Every page redesigned so it reads like a product, not a developer tool.
   Datum's rules for how a screen behaves, Apple's and car makers' sites for
   how it looks. Monochrome (the user's choice): no hue anywhere.
2. A Settings page in `zeroout run` where people add their keys and change
   their settings, including verifying their own phone number with Twilio
   (the user's choice), without the terminal.
3. Menu words that are easy to read: one sentence per line, and each option on
   its own line with its key beside it.

## What comes from Datum, and what does not

Taken, as rules every page follows:

- Every screen says what to do next, and the next step is one click away.
- The reason sits beside the thing it is about, never behind a hover or a fold.
- One word means one state, everywhere (the words table below).
- Errors are plain sentences that name the bad value and what is allowed.
  No exclamation marks. Never "invalid input".
- State is shown with a word and a shape (a filled or hollow dot, an inverted
  key), never by colour.
- Numbers use tabular figures so they line up.
- Every page ends in a `.note` that says why the product works this way and
  what to do next. Where a number is not real, the page says so; no invented
  figures (so the phone page shows no "step 2 of 5" unless it is computed).
- Dark mode is the same gray scale inverted, under both
  `prefers-color-scheme` and `data-theme`.

Not taken: Datum's look of a dense tool (uppercase mono 10 px labels, grids,
rails, status bars). Those are exactly what reads as a developer tool. No
monospace face anywhere, except where the thing shown really is a file (the
exact JSON before sharing), and that uses the system's `ui-monospace`.

## The look

Apple's product pages and car makers' sites (Porsche, Tesla, Rivian) share a
few habits, and Zero-Out takes them: a quiet translucent top bar, one centered
column, very large tight headlines, a lot of space, pill buttons in pure black
and white, tiles with large radii and no shadows, and one hero object per page.

**Zero-Out's hero object is the phone key.** The answer to "what do I press" is
shown as physical key caps, the way a car configurator shows its one spec.
On the phone page a full keypad lights the key to press now (inverted).
Everything else stays quiet so the keys are what people see.

### Tokens (`zeroout/static/app.css`)

- Gray scale: Datum's twelve steps `--g1..--g12`, exact values, light and dark,
  plus `--inv-bg` / `--inv-fg` (pure black and white). Aliases: `--app` page,
  `--tile` raised areas (g3 light), `--surface` grouped lists, `--line`,
  `--line-int`, `--text`, `--text-low`, `--text-dim`.
- Type: Inter 4.1, bundled in `zeroout/static/fonts/` (copied from Datum's
  `src/fonts`, OFL 1.1, licence file beside them), because the app is offline
  and its CSP allows only its own origin. Scale: 12, 14, 17 (body), 21 (lede),
  28, 40, 56 (display). Body line height 1.47; headlines 1.07 with tracking
  -0.025em to -0.03em. Sentence case everywhere; no all-caps labels.
- Radii by hierarchy, not one radius everywhere: 999 px pills for buttons,
  12 px fields, 14 px keys and notices, 16 px grouped lists, 22 px tiles.
- Width: `.page` 980 px, `.page.narrow` 692 px (reading width), 22 px gutters,
  16 px at 734 px and below. Touch targets 44 px minimum.
- Motion only in answer to an action (a disclosure opening, a key lighting).
  `prefers-reduced-motion` turns it off.

### Components (the class vocabulary every page uses)

| Class | What it is |
|---|---|
| `.bar` | Sticky top bar: wordmark left, `Companies` and `Settings` right; translucent with backdrop blur |
| `.page`, `.page.narrow` | Centered column |
| `.hero` with `h1.display` and `p.lede` | The page's opening: one big headline, one plain sentence |
| `.btn` + `.primary` / `.secondary` / `.quiet` / `.small` | Pill buttons: black on white, gray fill, text only, smaller |
| `.actions` | A row of buttons that wraps |
| `.field` with `label`, `input`/`select`, `.hint`, `.error-text` | A labelled form field; `.field.bad` marks the field the error is about |
| `.tile`, `.tiles` | A large rounded area; `.tiles` is the responsive grid of company tiles (each tile is one link) |
| `.list` with `li` / `.row` | Grouped list in the Apple Settings style: rounded, hairline separators |
| `.state.on` / `.state.off` / `.state.warn` + a word | A dot and a sentence-case word: filled, hollow, half |
| `.notice`, `.notice.strong` | A message beside its subject; `.strong` is inverted for things that stop work |
| `.note` | The closing argument at the end of every page |
| `.key`, `.key.sm`, `.key.lit`, `.keys` | A phone key cap; `.lit` is inverted; `.keys` is a row of keys joined by "then" |
| `.keypad` | The 3 by 4 keypad on the phone page |
| `.lines` | A transcript, one `li` per line; a line with an option starts with a `.key.sm` |
| `.seg` | Segmented control (Light, Dark, Match system) |
| `.meter` | The listening level (`#meter` with a `span` child, width set by phone.js) |
| `.mono` | Only for the exact file shown before sharing |

`base.html` links `/static/app.css` and `/static/theme.js`; pages keep no inline
styles. `theme.js` applies `data-theme` from `localStorage` key
`zeroout.theme` (a per-browser preference; wrapped in try/catch) and wires any
`[data-theme-set]` button.

## The words

| Word | Means, and only means |
|---|---|
| mapped | Zero-Out has heard this menu or option |
| not mapped yet | nobody has heard it |
| changed | the menu was different the last time it was heard; it is being checked again (replaces "stale" on screen) |
| confirmed | heard the same way again; with its date |
| Map with my phone | phone in hand mode |
| Map automatically | a Twilio crawl (was "Map this number") |
| Connect me | Zero-Out calls you, then the company, and presses the keys |
| option | one choice in a menu (replaces "branch" and "node" on screen) |
| Share / Shared | send what was mapped here to the community; always says it is public and permanent |
| Update from the community | bring in what others shared |
| Keep / Discard | what was heard on the phone page |

Not on screen: observation, node, tree, branch, outbox (said as "a folder on
this computer"), gh (said as "GitHub"). "Pull request" stays once, explained:
the warning that it is public and permanent stays word for word in substance.

## Menu words, line by line (item 3)

`zeroout/menu_text.py`, stdlib only (it is vendored into the trees repo's
`tools/` beside `community_check.py`, loaded by path the same way):

```python
menu_lines(text: str, keys: Iterable[str] = ()) -> list[dict]  # [{"text": str, "key": str | None}]
```

- Splits at sentence ends (`.` `?` `!` followed by space or end), keeping the
  punctuation; never inside a number or a redaction mark.
- A sentence with two or more key phrases ("For billing press 1, for support
  press 2") is split before each later clause, so every option is one line.
- `key` is the key a line asks for: "press 1", "push 2", "dial 3", "select 4",
  "enter 5", "say or press 6", "press star" / "*", "press pound" / "#", and the
  Spanish "oprima", "marque", "presione" with "uno" to "nueve", "cero",
  "asterisco", "numeral" / "gato". When `keys` is given, only those keys count.
- Text with no sentence marks comes back as one line. Empty text, an empty list.

Shown in three places: the phone page's Heard panel (the clip response gains
`heard.lines`), the menu on the company page (a Jinja filter `menu_lines`), and
the community site's pages.

## Settings (item 2)

Only in local `zeroout run` (404 when hosted), behind the existing launch
session and Origin check. `GET /settings` and one HTMX form per section, each
`POST` answering with its own section partial and a result sentence.

| Section | Fields | Checked by |
|---|---|---|
| Language model | Token Factory API key, base URL, Nano model, Super model | the live `/models` call setup makes; models chosen from that list |
| Phone calls (optional) | Twilio Account SID, auth token, your own number; Verify my number | the account fetch setup makes; the caller ID list |
| Speech | which installed speech model | the folder holds the model's files |
| Spending | daily spend cap in USD | a finite number above 0 |
| Community | community repo (owner/name); shows the GitHub account if `gh` is signed in | `valid_repo` |
| Appearance | Light, Dark, Match system | per browser, nothing sent |

Rules, each pinned by a test:

- A secret (API key, auth token) is never sent back to the browser: not in a
  value attribute, not in a message. The page says "Saved, ends in 4f2a". An
  empty secret field keeps the saved value.
- Saving writes the file the running app loaded (`ZEROOUT_ENV_FILE` when it is
  named, else the per-user `zeroout.env`) through setup's own `render()` and
  `write_config()` (0600 temp file, renamed over). Keys this page does not
  manage are kept. The page names the file.
- A key set in the real environment wins over the file, so the page shows it
  as read-only: "Set in your environment as TF_API_KEY; change it there."
- Line breaks are refused. The base URL must be https (or http only to
  localhost), so a key is never sent in the clear.
- Changes take effect without restarting: the running `Settings` is updated in
  place, and everything that read a setting at start reads it at call time.
  The session secret and cookie flag never change while running.
- Verify my number: Twilio calls the number, the page shows the code to type
  on the phone, and polls every 3 s for up to 120 s until Twilio lists it as a
  verified caller ID. One verification at a time.
- The same checks as `zeroout setup`, by sharing its functions, not copying
  them. `zeroout setup` keeps working; its tests stay green.

Not in Settings: downloading a speech model (470 MB, hash checked) stays in
`zeroout setup`; the page says so beside a model that is not installed.

## Pages

- **Home**: hero "Skip the phone menu." and one sentence; company tiles (name,
  number, mapped state); Add a company; Update from the community and the
  community's offered numbers; a notice when the Token Factory key is missing
  ("Add your Token Factory key in Settings to read menus") with the link.
- **Company**: name as the display headline, number below; Map with my phone
  (primary) and Map automatically; "What do you need?" with the answer as key
  caps; the menu as a grouped list with disclosures, each option's key cap,
  label, state word, transcript lines, and Connect me; Connect me's setup;
  Share; Changes.
- **Phone**: one task at a time. The instruction is the headline ("Press 1 now"),
  the keypad lights that key, Start listening is the one primary button, the
  meter while listening, then Heard as transcript lines and options with keys,
  and Keep / Discard. Every element id and data attribute phone.js and the
  browser test use stays.
- **Settings**: as above.
- **Expired link**: a real page in the same design saying how to get a new
  link (run `zeroout run` again).
- **Community site** (`site_build.py`): the same tokens and type in one inline
  stylesheet (the site is static and has no `/static`), transcript lines and
  key caps, index of numbers, one page per number.

## Who does what (the user's routing rule)

- Opus: this plan, `app.css`, `theme.js`, `base.html` and the fonts;
  coordination, integration, the security review of Settings and the approval
  review.
- Sonnet engineers, on disjoint files:
  - A: home, company, the partials, the expired page; `test_web.py` follows the words.
  - B: phone page and phone.js's display code (not its recorder or endpointer).
  - C: Settings, server and page, with new tests.
  - D: `menu_text.py` with tests, its two hookups in `web.py` (the filter and
    `heard.lines`), `site_build.py`'s look, and the trees repo's `tools/` copies.
- Haiku: a words pass against the table above, and formatting.

## Verification

- The full test suite and the node tests, nothing skipped.
- Screenshots of every page in light and dark, at 1280 and 390 px wide,
  reviewed before commit.
- The phone flow again in real Chrome with the fake microphone.
- Settings against the live Token Factory key (the key is never printed).

## Done

- 2026-10-25, done. 1318 Python tests and the node recorder tests pass, none skipped.
- Checked in real Chrome: screenshots of home, company, phone, Settings and the error page, in light and dark at 1280 and 390 px, with two rounds of fixes. The phone flow ran twice with the fake microphone, Parakeet and live Nano: the full menu was heard, shown one line per sentence with key caps, kept, and the next instruction was given.
- Added beyond the plan, from that run: Nano sometimes returned only the greeting as prompt_text while finding every option. listen.py's prompt now asks for every sentence, and when the model's text covers under half the words of the lines up to the end of the menu, those lines are used instead (menus only; repeats are excluded).
- Added from the screenshot review: full pages for non-htmx errors and for the Connect me result (message.html); Map automatically is disabled with "Needs Twilio. Add it in Settings." and the crawl route refuses without Twilio; a `when` filter shows dates as "Oct 25, 2026"; Settings can stop using Twilio and checks the saved number with Twilio when the page opens.
- Security review (Opus) found 2 medium, 4 low and 2 later edge cases, all fixed with tests and re-probed: the Token Factory key no longer follows redirects; Stop using Twilio clears the keys in every loaded config file and checks after reloading; bad base URLs give a sentence instead of a 500; background reads have their own rate limit and cross-site GETs to Settings are refused; the verify poll is a POST; the sentence-end regex is linear; write_config writes through symlinks; a partial write names what changed.
- Approval review (Opus) found 1 high (a Unicode case-folding KeyError in menu_lines that one shared file could use to break the site build), 1 medium ("select one of the following" read as key 1) and 4 low, all fixed; approved on re-review.
- To watch: maps stored before this change may hold greeting-only text; the next crawl or phone clip compares it with the full text and may call the menu changed. No stored maps existed on the development machine.
