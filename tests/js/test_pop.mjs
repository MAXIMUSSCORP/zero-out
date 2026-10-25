// Run with: node tests/js/test_pop.mjs  (exits non-zero on failure)
import assert from 'node:assert/strict';
import { createRequire } from 'node:module';

const { place } = createRequire(import.meta.url)('../../zeroout/static/pop.js');

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
// a window narrower than the popover still keeps the left margin
assert.equal(place(btn(100, 200), size, { w: 300, h: 800 }).left, 16);
console.log('ok');
