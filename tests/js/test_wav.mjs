// Run with: node tests/js/test_wav.mjs  (exits non-zero on failure)
import assert from 'node:assert/strict';
import { createRequire } from 'node:module';

const phone = createRequire(import.meta.url)('../../zeroout/static/phone.js');

const ascii = (v, off, n) => String.fromCharCode(...new Uint8Array(v.buffer, off, n));

// header and samples
const wav = phone.encodeWav(Float32Array.from([0, 1, -1, 0.5, 2, -2]), 16000);
const v = new DataView(wav);
assert.equal(wav.byteLength, 44 + 12);
assert.equal(ascii(v, 0, 4), 'RIFF');
assert.equal(v.getUint32(4, true), wav.byteLength - 8);
assert.equal(ascii(v, 8, 8), 'WAVEfmt ');
assert.equal(v.getUint32(16, true), 16);
assert.equal(v.getUint16(20, true), 1);
assert.equal(v.getUint16(22, true), 1);
assert.equal(v.getUint32(24, true), 16000);
assert.equal(v.getUint32(28, true), 32000);
assert.equal(v.getUint16(32, true), 2);
assert.equal(v.getUint16(34, true), 16);
assert.equal(ascii(v, 36, 4), 'data');
assert.equal(v.getUint32(40, true), 12);
const pcm = [0, 1, 2, 3, 4, 5].map((i) => v.getInt16(44 + i * 2, true));
assert.deepEqual(pcm, [0, 32767, -32768, 16384, 32767, -32768]);

// empty input still gives a valid header
assert.equal(phone.encodeWav(new Float32Array(0), 16000).byteLength, 44);

// downsample 48k to 16k averages groups of three
const down = phone.downsample(Float32Array.from([0, 0.3, 0.6, 1, 1, 1, -1, 0, 1]), 48000, 16000);
assert.equal(down.length, 3);
assert.ok(Math.abs(down[0] - 0.3) < 1e-6 && Math.abs(down[1] - 1) < 1e-6 && Math.abs(down[2]) < 1e-6);
// same or lower rate is a copy
assert.deepEqual(Array.from(phone.downsample(Float32Array.from([1, 2]), 16000, 16000)), [1, 2]);

// rms
assert.equal(phone.rms(new Float32Array(0)), 0);
assert.ok(Math.abs(phone.rms(Float32Array.from([0.5, -0.5])) - 0.5) < 1e-6);

// endpointer: run a list of per-frame levels, return the index where it stopped (or -1) and its flags
function run(levels) {
  const ep = phone.makeEndpointer(0.05);
  let stopped = -1;
  levels.forEach((l, i) => { if (stopped < 0 && ep.push(l)) stopped = i; });
  return { stopped, capped: ep.capped, sound: ep.sound };
}
const rep = (level, seconds) => Array(Math.round(seconds / 0.05)).fill(level);

// quiet lead-in, a menu, then 5 s of quiet stops 5 s after the speech ends
let r = run([...rep(0.002, 1), ...rep(0.2, 2), ...rep(0.002, 8)]);
assert.equal(r.stopped, 20 + 40 + 100 - 1);
assert.deepEqual([r.capped, r.sound], [false, true]);
// starting mid-speech (no quiet opening window) still hears speech and stops 5 s after it ends
r = run([...rep(0.2, 2), ...rep(0.002, 8)]);
assert.equal(r.stopped, 40 + 100 - 1);
assert.deepEqual([r.capped, r.sound], [false, true]);
// a 3.5 s pause inside a menu does not stop it
r = run([...rep(0.002, 1), ...rep(0.2, 2), ...rep(0.002, 3.5), ...rep(0.2, 2), ...rep(0.002, 8)]);
assert.equal(r.stopped, 20 + 40 + 70 + 40 + 100 - 1);
// hold music at a constant level never looks like silence: caps at 90 s with sound
r = run(rep(0.2, 120));
assert.equal(r.stopped, 1799);
assert.deepEqual([r.capped, r.sound], [true, true]);
// silence alone caps at 90 s with no sound
r = run(rep(0.002, 120));
assert.equal(r.stopped, 1799);
assert.deepEqual([r.capped, r.sound], [true, false]);
// stopping by hand is not a cap
const manual = phone.makeEndpointer(0.05);
manual.push(0.2);
assert.deepEqual([manual.capped, manual.sound], [false, true]);

// steady sound below the old 0.06 threshold is still sound and never looks like silence
r = run(rep(0.04, 120));
assert.equal(r.stopped, 1799);
assert.deepEqual([r.capped, r.sound], [true, true]);
// speech at 0.04 that starts mid-prompt stops 5 s after it ends
r = run([...rep(0.04, 2), ...rep(0.002, 8)]);
assert.equal(r.stopped, 40 + 100 - 1);
assert.deepEqual([r.capped, r.sound], [false, true]);
// the floor is a rolling window: a low floor from early silence ages out, so steady 0.015 room noise
// that follows speech counts as quiet once the 3 s window no longer holds the early silence
r = run([...rep(0.001, 2), ...rep(0.2, 1), ...rep(0.015, 8)]);
assert.equal(r.stopped, 193);
// the floor is the 10th percentile, not the minimum: single outliers of 0.001 do not hold it down
const outliers = rep(0.015, 8).map((l, i) => (i % 20 === 10 ? 0.001 : l));
r = run([...rep(0.2, 1), ...outliers]);
assert.equal(r.stopped, 20 + 100 - 1);

// ---------- controller ----------
function deferred() {
  let resolve, reject;
  const promise = new Promise((res, rej) => { resolve = res; reject = rej; });
  return { promise, resolve, reject };
}
function harness(over = {}) {
  const log = { record: 0, uploads: [], sent: [], busy: [], errors: [], nexts: [], reviews: [] };
  const rec = deferred(), decision = deferred();
  const io = {
    record: () => { log.record += 1; return rec.promise; },
    upload: async (path, r) => { log.uploads.push(path); return { heard: { prompt_text: 'hi', options: [] }, nonce: 'n1', message: 'check' }; },
    decide: () => decision.promise,
    send: async (name, payload) => { log.sent.push([name, payload]); return { message: 'ok', options: [], next: { path: '2', keys: ['2'], press_now: '2', label: 'x' }, skipped: 0 }; },
    ui: {
      busy: (on) => log.busy.push(on), error: (t) => log.errors.push(t), status: () => {},
      heard: () => {}, next: (step, skipped) => log.nexts.push([step, skipped]), review: (on) => log.reviews.push(on),
    },
    ...over,
  };
  const ctl = phone.makeController(io);
  ctl.current = { path: '1', keys: ['1'], press_now: '1', label: 'a' };
  return { ctl, log, rec, decision };
}

// the path posted is the one captured at Start, even if the page's step changes before the upload
{
  const { ctl, log, rec, decision } = harness();
  const running = ctl.run();
  ctl.current = { path: '2', keys: ['2'], press_now: '2', label: 'b' };
  rec.resolve({ samples: new Float32Array(0), rate: 16000, capped: false, sound: true });
  await new Promise((r) => setImmediate(r));
  assert.deepEqual(log.uploads, ['1']);
  decision.resolve('keep');
  await running;
  assert.deepEqual(log.sent, [['keep', { nonce: 'n1' }]]);
}

// Start stays disabled from the click until the Keep/Discard decision has settled
{
  const { ctl, log, rec, decision } = harness();
  const running = ctl.run();
  assert.equal(ctl.busy, true);
  await ctl.run();  // a second click while busy does nothing
  assert.equal(log.record, 1);
  rec.resolve({ samples: new Float32Array(0), rate: 16000, capped: false, sound: true });
  await new Promise((r) => setImmediate(r));
  assert.equal(ctl.busy, true);
  assert.deepEqual(log.reviews, [true]);
  decision.resolve('discard');
  await running;
  assert.equal(ctl.busy, false);
  assert.deepEqual(log.busy, [true, false]);
  assert.deepEqual(log.reviews, [true, false]);
  assert.equal(log.nexts.at(-1)[0].path, '2');
}

// nothing heard: no review, Start comes back, the retry step is shown with the skipped count
{
  const { ctl, log, rec } = harness({
    upload: async () => ({ heard: null, message: 'nothing', next: { path: '1', keys: ['1'], press_now: '1', label: 'a' }, skipped: 3 }),
  });
  const running = ctl.run();
  rec.resolve({ samples: new Float32Array(0), rate: 16000, capped: true, sound: false });
  await running;
  assert.equal(ctl.busy, false);
  assert.deepEqual(log.reviews, []);
  assert.equal(log.nexts.at(-1)[1], 3);
}

// errors re-enable Start and show the sentence; a denied microphone has its own sentence
{
  const { ctl, log, rec } = harness({ upload: async () => { throw new Error('Still hearing the last clip.'); } });
  const running = ctl.run();
  rec.resolve({ samples: new Float32Array(0), rate: 16000, capped: false, sound: true });
  await running;
  assert.equal(ctl.busy, false);
  assert.deepEqual(log.errors.filter(Boolean), ['Still hearing the last clip.']);
}
{
  const { ctl, log, rec } = harness();
  const running = ctl.run();
  rec.reject(Object.assign(new Error('x'), { name: 'NotAllowedError' }));
  await running;
  assert.equal(ctl.busy, false);
  assert.deepEqual(log.errors.filter(Boolean),
    ['Zero-Out needs the microphone to hear the menu. Allow it in the browser and try again.']);
}

// skip posts the current path, and is ignored at the root or while busy
{
  const { ctl, log } = harness();
  await ctl.skip();
  assert.deepEqual(log.sent, [['skip', { path: '1' }]]);
  ctl.current = { path: '', keys: [], press_now: null, label: '' };
  await ctl.skip();
  assert.equal(log.sent.length, 1);
}

// session expiry text
assert.equal(phone.EXPIRED_TEXT,
  'This Zero-Out session has ended. Open it again from the link that zeroout run printed.');
assert.equal(phone.nextText(null, 2), 'Every option Zero-Out could reach is mapped or was skipped. Hang up.');
assert.equal(phone.nextText(null, 0), 'This menu is mapped as far as Zero-Out goes. Hang up.');

// next-step text
assert.equal(phone.nextText(null), 'This menu is mapped as far as Zero-Out goes. Hang up.');
assert.equal(phone.nextText({ keys: ['2'], press_now: '2', label: 'billing' }),
  'Billing. Then click Start listening.');
assert.equal(phone.nextText({ keys: [], press_now: null, label: '' }),
  'Click Start listening when it answers.');
assert.equal(phone.nextText({ keys: ['1', '2'], press_now: null, label: 'x' }),
  'Hang up and call again, waiting for each menu to finish. Click Start listening right after your last key.');

// keypad lighting and headline
assert.equal(phone.litKey(null), '');
assert.equal(phone.litKey({ press_now: '2', keys: ['1', '2'] }), '2');
assert.equal(phone.litKey({ press_now: '*', keys: ['*'] }), '*');
assert.equal(phone.litKey({ press_now: null, keys: ['1', '2'] }), '1');
assert.equal(phone.litKey({ press_now: null, keys: [] }), '');
assert.equal(phone.headlineText({ press_now: '3', keys: ['3'] }), 'Press 3 now');
assert.equal(phone.headlineText({ press_now: null, keys: ['1', '2'] }), 'Press 1, then 2');
assert.equal(phone.headlineText({ press_now: null, keys: [] }), 'Call the number');
assert.equal(phone.headlineText(null), 'Hang up');

// heard lines: the server's lines, else the prompt as one line, else nothing
assert.deepEqual(phone.heardLines('a b', [{ text: 'a', key: null }]), [{ text: 'a', key: null }]);
assert.deepEqual(phone.heardLines('a b', undefined), [{ text: 'a b', key: null }]);
assert.deepEqual(phone.heardLines('a b', []), [{ text: 'a b', key: null }]);
assert.deepEqual(phone.heardLines('', null), []);

// renderLines on a minimal fake DOM: text only, a key cap only for a keyed line
function fakeEl(tag) {
  const el = { tag, className: '', children: [], _text: '', innerHTMLSet: false };
  el.appendChild = (c) => { el.children.push(c); return c; };
  Object.defineProperty(el, 'textContent', {
    get: () => el._text + el.children.map((c) => c.textContent).join(''),
    set: (v) => { el._text = v; el.children = []; },
  });
  Object.defineProperty(el, 'innerHTML', { set: () => { el.innerHTMLSet = true; } });
  return el;
}
const doc = { createElement: fakeEl };
const list = fakeEl('ol');
list.textContent = 'old';
phone.renderLines(doc, list, [{ text: 'For billing press 1.', key: '1' }, { text: '<b>hi</b>', key: null }]);
assert.equal(list.children.length, 2);
const [keyed, plain] = list.children;
assert.deepEqual(keyed.children.map((c) => [c.className, c.textContent]), [['key sm', '1'], ['vh', ': '], ['label', 'For billing press 1.']]);
assert.equal(keyed.textContent, '1: For billing press 1.');
assert.deepEqual(plain.children.map((c) => [c.className, c.textContent]), [['label', '<b>hi</b>']]);
assert.ok(list.textContent.includes('For billing press 1.') && list.textContent.includes('<b>hi</b>'));
assert.equal(list.innerHTMLSet, false);
phone.renderLines(doc, list, []);
assert.equal(list.children.length, 0);

// the transcript is a quote: text only, no key caps, even for a keyed line
const box = fakeEl('div');
box.textContent = 'old';
phone.renderQuote(doc, box, [{ text: 'For billing press 1.', key: '1' }, { text: '<b>hi</b>', key: null }]);
assert.deepEqual(box.children.map((c) => [c.tag, c.className, c.textContent]),
  [['li', '', 'For billing press 1.'], ['li', '', '<b>hi</b>']]);
assert.ok(box.children.every((c) => c.children.length === 0));
assert.ok(box.textContent.includes('For billing press 1.') && box.innerHTMLSet === false);
phone.renderQuote(doc, box, []);
assert.equal(box.children.length, 0);

// options read 1-9, 0, *, #
const keysOf = (o) => phone.sortOptions(o).map((x) => x.key);
assert.deepEqual(keysOf([{ key: '#' }, { key: '0' }, { key: '*' }, { key: '9' }, { key: '1' }]), ['1', '9', '0', '*', '#']);
assert.deepEqual(keysOf([]), []);
assert.deepEqual(keysOf(undefined), []);
const given = [{ key: '2' }, { key: '1' }];
phone.sortOptions(given);
assert.deepEqual(given.map((x) => x.key), ['2', '1']);  // the input is not reordered in place

// a speech menu changes the headline; nothing else does
assert.equal(phone.heardHeadline('speech_menu'), 'This menu wants you to speak');
assert.equal(phone.heardHeadline('menu'), '');
assert.equal(phone.heardHeadline(undefined), '');

// the page as opened: how to start, never "again"; the call instruction appears once, in #next
const open = { fresh: true, number: '1-800-555-0142' };
const deep = { path: '2.1', keys: ['2', '1'], press_now: null, label: 'costco city visa' };
const root = { path: '', keys: [], press_now: null, label: '' };
assert.equal(phone.headlineText(deep, open), 'Map 2, then 1');
assert.equal(phone.headlineText(root, open), 'Map the main menu');
assert.equal(phone.headlineText(null, open), 'Everything reachable is mapped');
assert.equal(phone.ledeText(deep, open), 'Costco city visa');
assert.equal(phone.ledeText({ ...deep, label: ' ' }, open), 'Call 1-800-555-0142 on your phone, on speaker, next to this computer.');
assert.equal(phone.ledeText(root, open), 'Call 1-800-555-0142 on your phone, on speaker, next to this computer.');
assert.equal(phone.ledeText(null, open), 'Call only if you want to check whether the menu has changed.');
assert.equal(phone.ledeText(deep, { number: '1-800-555-0142' }), 'Call 1-800-555-0142 on your phone, on speaker, next to this computer.');
assert.equal(phone.ledeText({ ...deep, label: 'y'.repeat(120) }, open).length, 90);
assert.equal(phone.nextText(deep, 0, open),
  'Call 1-800-555-0142, press 2, then 1, waiting for each menu to finish, then click Start listening right after your last key.');
assert.equal(phone.nextText(root, 0, open), 'Click Start listening when it answers.');
assert.equal(phone.nextText(null, 0, open),
  'Zero-Out has heard every option it can reach. Options that need a person, account details or speech, that replay a menu, or that sit more than four menus deep are left out.');
assert.equal(phone.nextText(null, 2, open), 'Every option Zero-Out could reach is mapped or was skipped. Hang up.');
// after an action the headline holds the keys and #next the instruction, without repeating either
assert.equal(phone.headlineText(deep), 'Press 2, then 1');
assert.equal(phone.nextText(deep, 0),
  'Hang up and call again, waiting for each menu to finish. Click Start listening right after your last key.');
assert.ok(!/again|hang up/i.test(phone.nextText(deep, 0, open) + phone.headlineText(deep, open)));
assert.ok(/again/i.test(phone.nextText(deep, 0)));
assert.ok(!phone.nextText(deep, 0).includes('press 2') && !/press 2/i.test(phone.nextText(deep, 0)));
// a press_now step is only offered after a clip was kept, and keeps its own words
const now = { path: '1', keys: ['1'], press_now: '1', label: 'billing' };
assert.equal(phone.headlineText(now, open), 'Press 1 now');
assert.equal(phone.headlineText(now), 'Press 1 now');
assert.equal(phone.nextText(now, 0), 'Billing. Then click Start listening.');
assert.equal(phone.nextText({ ...now, label: '' }, 0), 'Click Start listening.');
assert.equal(phone.nextText(root, 0), 'Click Start listening when it answers.');
assert.equal(phone.headlineText(root), 'Call the number');
// labels: capitalized, trimmed, at most 60 characters with an ellipsis, text only
assert.equal(phone.shortLabel(' hello '), 'Hello');
assert.equal(phone.shortLabel('x'.repeat(80)).length, 60);
assert.ok(phone.shortLabel('x'.repeat(80)).endsWith('\u2026'));
assert.equal(phone.shortLabel('y'.repeat(60)), 'Y' + 'y'.repeat(59));
assert.equal(phone.shortLabel(undefined), '');
assert.equal(phone.shortLabel('x'.repeat(200), 90).length, 90);

console.log('ok');
