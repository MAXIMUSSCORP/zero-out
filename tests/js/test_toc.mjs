// Run with: node tests/js/test_toc.mjs  (exits non-zero on failure)
import assert from 'node:assert/strict';
import { createRequire } from 'node:module';

const { pick, stillPinned, afterPointerDown } = createRequire(import.meta.url)('../../zeroout/static/toc.js');

const at = (...tops) => tops.map((top, i) => ({ id: 's' + i, top }));

assert.equal(pick([], 800, 5000), null);
assert.equal(pick(null, 800, 5000), null);
// at the top of the page the first section is current, whatever else is below
assert.equal(pick(at(0, 500, 1200), 800, 5000), 's0');
assert.equal(pick(at(300, 900), 800, 5000), 's0', 'before any top has crossed, the first stays current');
// a section becomes current once its top is above 30% of the viewport (240 of 800)
assert.equal(pick(at(-600, 240, 900), 800, 5000), 's1');
assert.equal(pick(at(-600, 241, 900), 800, 5000), 's0');
assert.equal(pick(at(-2000, -900, -10, 700), 800, 5000), 's2');
// the line slides from 30% down to the bottom of the viewport over the last viewport of scrolling:
// with a full viewport or more still to go it is at 30%; at the very end it is at the bottom
assert.equal(pick(at(-300, 500, 900), 800, 800), 's0');
assert.equal(pick(at(-300, 500, 900), 800, 5000), 's0');
assert.equal(pick(at(-300, 700, 900), 800, 0), 's1', 'at the end every section whose top is in view counts');
assert.equal(pick(at(-300, 700, 790), 800, 0), 's2');
assert.equal(pick(at(-300, 700, 810), 800, 0), 's1');
// halfway through the last viewport the line is at 30% + 70% * 0.5 = 65% (520 of 800)
assert.equal(pick(at(-900, 520, 700), 800, 400), 's1');
assert.equal(pick(at(-900, 521, 700), 800, 400), 's0');

// scrolling from the top to the very bottom of a page whose last sections are short activates every section,
// in order, and never skips one
const DOC = 3100, VH = 800, ABS = [0, 700, 1500, 2300, 2550, 2800];
const seen = [];
for (let y = 0; y <= DOC - VH; y += 10) {
  const id = pick(ABS.map((t, i) => ({ id: 's' + i, top: t - y })), VH, DOC - (y + VH));
  if (seen[seen.length - 1] !== id) seen.push(id);
}
assert.deepEqual(seen, ['s0', 's1', 's2', 's3', 's4', 's5']);
// the same with two tiny sections squeezed in the last 300 px
const seen2 = [];
const ABS2 = [0, 900, 1800, 2400, 2560, 2700];
for (let y = 0; y <= 3000 - VH; y += 5) {
  const id = pick(ABS2.map((t, i) => ({ id: 's' + i, top: t - y })), VH, 3000 - (y + VH));
  if (seen2[seen2.length - 1] !== id) seen2.push(id);
}
assert.deepEqual(seen2, ['s0', 's1', 's2', 's3', 's4', 's5']);

// short pages: less than a viewport of total scroll. The slide is measured over what can be scrolled (y is
// how far the page has scrolled, remaining how far it still can), so y = 0 is always the first section.
assert.equal(pick(at(300, 620), 900, 500, 0), 's0', 'the home page opens on its first section');
assert.equal(pick(at(300, 620), 900, 0, 0), 's0', 'a page that does not scroll at all opens on its first section');
assert.equal(pick(at(0, 120, 400), 900, 0, 0), 's0');
assert.equal(pick(at(300, 620), 900, 0, 500), 's1', 'at the bottom of a short page the last section is current');
assert.equal(pick(at(-200, 120), 900, 300, 200), 's1');
assert.equal(pick(at(300, 620), 900, 800, undefined), 's0', 'without y the old measure applies');
{
  const total = 500, vh = 900, abs = [300, 620];
  const seenShort = [];
  for (let y = 0; y <= total; y += 5) {
    const id = pick(abs.map((t, i) => ({ id: 's' + i, top: t - y })), vh, total - y, y);
    if (seenShort[seenShort.length - 1] !== id) seenShort.push(id);
  }
  assert.deepEqual(seenShort, ['s0', 's1']);
  // four sections on a page that scrolls 300 px of an 800 px viewport: every one takes its turn, in order
  const seen4 = [];
  const abs4 = [100, 350, 600, 780];
  for (let y = 0; y <= 300; y += 2) {
    const id = pick(abs4.map((t, i) => ({ id: 's' + i, top: t - y })), 800, 300 - y, y);
    if (seen4[seen4.length - 1] !== id) seen4.push(id);
  }
  assert.deepEqual(seen4, ['s0', 's1', 's2', 's3']);
}

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
