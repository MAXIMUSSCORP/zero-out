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
assert.equal(phone.nextText(null, 2), 'Every branch Zero-Out could reach is mapped or was skipped. Hang up.');
assert.equal(phone.nextText(null, 0), 'This menu is mapped as far as Zero-Out goes. Hang up.');

// next-step text
assert.equal(phone.nextText(null), 'This menu is mapped as far as Zero-Out goes. Hang up.');
assert.equal(phone.nextText({ keys: ['2'], press_now: '2', label: 'billing' }),
  'Press 2 now (billing). Then click Start listening.');
assert.equal(phone.nextText({ keys: [], press_now: null, label: '' }),
  'Call the number. Click Start listening when it answers.');
assert.equal(phone.nextText({ keys: ['1', '2'], press_now: null, label: 'x' }),
  'Hang up and call again. Press 1, then 2, waiting for each menu to finish. Click Start listening right after your last key.');

console.log('ok');
