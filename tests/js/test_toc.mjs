// Run with: node tests/js/test_toc.mjs  (exits non-zero on failure)
import assert from 'node:assert/strict';
import { createRequire } from 'node:module';

const { pick, stillPinned, afterPointerDown } = createRequire(import.meta.url)('../../zeroout/static/toc.js');

const at = (...tops) => tops.map((top, i) => ({ id: 's' + i, top }));

assert.equal(pick([], 800, false), null);
assert.equal(pick(null, 800, false), null);
// at the top of the page the first section is current, whatever else is below
assert.equal(pick(at(0, 500, 1200), 800, false), 's0');
assert.equal(pick(at(300, 900), 800, false), 's0', 'before any top has crossed, the first stays current');
// a section becomes current once its top is above 30% of the viewport (240 of 800)
assert.equal(pick(at(-600, 240, 900), 800, false), 's1');
assert.equal(pick(at(-600, 241, 900), 800, false), 's0');
assert.equal(pick(at(-2000, -900, -10, 700), 800, false), 's2');
// at the bottom the last section counts when its top is in view, even if it never reached the line
assert.equal(pick(at(-900, -300, 500), 800, true), 's2');
// but not when it is below the viewport: the one closest above the 30% line wins
assert.equal(pick(at(-900, 100, 900), 800, true), 's1');
// and a page that merely bottoms out while a click scrolls (atBottom false) uses the 30% line
assert.equal(pick(at(-900, -300, 500), 800, false), 's1');

// a clicked entry stays pinned while the page is still scrolling toward it
const pin = { id: 's3', dest: 2000, settled: false };
assert.equal(stillPinned(pin, 0), pin);
assert.equal(stillPinned(pin, 1200), pin);
// once settled near the destination it holds, and a move of more than 40 px away releases it
const settled = stillPinned(pin, 1980);
assert.deepEqual(settled, { id: 's3', dest: 2000, settled: true });
assert.deepEqual(stillPinned(settled, 1965), { id: 's3', dest: 2000, settled: true });
assert.equal(stillPinned(settled, 1950), null);
assert.equal(stillPinned(settled, 2100), null);
assert.equal(stillPinned(null, 5), null);

// a press on the page (the scrollbar, the text) releases the pin; a press on a toc link keeps it
assert.equal(afterPointerDown(settled, false), null);
assert.equal(afterPointerDown(settled, true), settled);
console.log('ok');
