// Run with: node tests/js/test_pop.mjs  (exits non-zero on failure)
import assert from 'node:assert/strict';
import { createRequire } from 'node:module';

const { place, next } = createRequire(import.meta.url)('../../zeroout/static/pop.js');

const view = { w: 1280, h: 800 };
const size = { w: 320, h: 120 };
const btn = (left, top) => ({ left, top, bottom: top + 22 });

// below the button, left aligned, 8 px apart
assert.deepEqual(place(btn(300, 200), size, view), { top: 230, left: 300 });
// no room below: flips above
assert.deepEqual(place(btn(300, 700), size, view), { top: 700 - 8 - 120, left: 300 });
// no room above either (a tall popover in a short window): stays below, kept inside the window
assert.deepEqual(place(btn(300, 40), { w: 320, h: 760 }, view), { top: 800 - 760 - 16, left: 300 });
// clamped from the right edge
assert.deepEqual(place(btn(1200, 200), size, view), { top: 230, left: 1280 - 320 - 16 });
// clamped from the left edge
assert.deepEqual(place(btn(2, 200), size, view), { top: 230, left: 16 });
// vertical: a tall popover near the bottom moves up to fit
assert.deepEqual(place(btn(300, 760), { w: 320, h: 500 }, view), { top: 760 - 8 - 500, left: 300 });
// one taller than the window starts at the top margin (the stylesheet scrolls it from there)
assert.equal(place(btn(300, 400), { w: 320, h: 900 }, view).top, 16);
// one that fits below stays below even when it nearly touches the bottom margin
assert.equal(place(btn(300, 650), { w: 320, h: 100 }, view).top, 680);
// a window narrower than the popover still keeps the left margin
assert.equal(place(btn(100, 200), size, { w: 300, h: 800 }).left, 16);

// ---------- hover, focus, click ----------
const closed = { open: false, pinned: false };
const run = (state, ...evs) => evs.reduce((st, ev) => ({ ...st, ...next(st, ev) }), state);
// hovering schedules an open, which then opens; leaving schedules a close, which then closes
assert.equal(next(closed, 'enter').act, 'schedule-open');
assert.equal(next(closed, 'timer-open').act, 'open');
let st = run(closed, 'enter', 'timer-open');
assert.deepEqual([st.open, st.pinned], [true, false]);
assert.equal(next(st, 'leave').act, 'schedule-close');
assert.equal(next(st, 'enter').act, 'cancel', 'moving onto the popover keeps it open');
st = run(st, 'leave', 'timer-close');
assert.deepEqual([st.open, st.pinned], [false, false]);
// leaving before the open timer fires cancels it
assert.equal(next(closed, 'leave').act, 'cancel');
// a click pins it, from closed or from hover-open; a pinned one ignores leave and the close timer
st = run(closed, 'click');
assert.deepEqual([st.open, st.pinned, st.act], [true, true, 'open']);
assert.equal(next(st, 'leave').act, null);
assert.equal(next(st, 'timer-close').act, null);
st = run(closed, 'enter', 'timer-open', 'click');
assert.deepEqual([st.open, st.pinned], [true, true]);
// clicking a pinned one closes it
st = run(st, 'click');
assert.deepEqual([st.open, st.pinned, st.act], [false, false, 'close']);
// keyboard focus opens it, blur closes it unless pinned
st = run(closed, 'focus');
assert.deepEqual([st.open, st.act], [true, 'open']);
assert.equal(next(st, 'blur').act, 'close');
assert.equal(next(run(st, 'click'), 'blur').act, null);
// the browser closing it (Escape, a click away) resets everything
st = run(closed, 'click', 'closed');
assert.deepEqual([st.open, st.pinned], [false, false]);
// unknown events change nothing
assert.deepEqual(next(st, 'wat'), { open: false, pinned: false, act: null });
console.log('ok');
