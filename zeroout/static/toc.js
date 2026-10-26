/* The table of contents at the left of a page: marks the section being read. The links are
   server rendered and work without this file; this only adds the current mark. */
(function () {
  'use strict';

  /* ---------- which section is current ---------- */

  // items: [{id, top}] in page order, top being the section's distance from the viewport's top; remaining is
  // how far the page can still scroll. The current section is the last whose top is above an activation line.
  // The line sits at 30% of the viewport, and over the last viewport of scrolling slides down to the bottom
  // edge, so short sections at the end of a page take their turn instead of being skipped. y is how far the
  // page has scrolled: on a page with less than a viewport to scroll the slide is measured over that shorter
  // span, so it starts at 0 and the first section is current on load.
  function pick(items, viewH, remaining, y) {
    if (!items || !items.length) return null;
    if (y === 0) return items[0].id;  // nothing scrolled yet, so the first section is the one being read
    var left = Math.max(0, remaining);
    var span = typeof y === 'number' ? Math.min(viewH, y + left) : viewH;
    var slide = viewH > 0 && span > 0 ? Math.min(1, Math.max(0, 1 - left / span)) : 0;
    var line = viewH * 0.3 + viewH * 0.7 * slide, cur = items[0].id;
    for (var i = 0; i < items.length; i++) if (items[i].top <= line) cur = items[i].id;
    return cur;
  }

  // A clicked entry stays current until the person scrolls on their own: pin is {id, dest, settled}, dest
  // the scroll position the click is heading for. Once the page has settled there, moving more than
  // 40 px away from it releases the pin.
  function stillPinned(pin, y) {
    if (!pin) return null;
    if (Math.abs(y - pin.dest) <= 40) return { id: pin.id, dest: pin.dest, settled: true };
    return pin.settled ? null : pin;
  }

  // A press anywhere lets go of a pin (a drag on the scrollbar, a click in the text), except on a toc link,
  // whose own click is what sets the pin.
  function afterPointerDown(pin, onTocLink) {
    return onTocLink ? pin : null;
  }

  // An index of one entry is not an index: the nav shows only when two or more entries are usable.
  function enough(usable) {
    return usable >= 2;
  }

  var api = { enough: enough, pick: pick, stillPinned: stillPinned, afterPointerDown: afterPointerDown };
  if (typeof module !== 'undefined') module.exports = api;
  if (typeof document === 'undefined') return;

  /* ---------- page ---------- */

  try {
    var nav = document.querySelector('nav.toc');
    if (!nav) return;
    var entries = [], queued = false, io = null, mo = null, pin = null;

    var target = function (e) { return document.getElementById(e.id); };
    var usable = function (t) { return !!t && !t.hidden && t.getClientRects().length > 0; };

    function collect() {
      entries = Array.prototype.map.call(nav.querySelectorAll('a[href^="#"]'), function (a) {
        var id = '';
        try { id = decodeURIComponent(a.getAttribute('href').slice(1)); } catch (e) { id = a.getAttribute('href').slice(1); }
        return { a: a, li: a.parentNode, id: id };
      });
      if (io) io.disconnect();
      if (mo) mo.disconnect();
      entries.forEach(function (e) {
        var t = target(e);
        if (!t) return;
        if (io) io.observe(t);
        if (mo) mo.observe(t, { attributes: true, attributeFilter: ['hidden'] });
      });
    }

    function mark(id) {
      entries.forEach(function (e) {
        if (e.id === id && !e.li.hidden) {
          e.a.setAttribute('aria-current', 'location');
        } else {
          e.a.removeAttribute('aria-current');
        }
      });
    }

    function update() {
      queued = false;
      try {
        // A section that is hidden or gone (the phone page's Heard, before there is anything heard) loses its entry.
        entries.forEach(function (e) { e.li.hidden = !usable(target(e)); });
        var root = document.documentElement, y = window.pageYOffset || root.scrollTop || 0;
        pin = stillPinned(pin, y);
        if (pin && !entries.some(function (e) { return e.id === pin.id && !e.li.hidden; })) pin = null;
        if (pin) { mark(pin.id); return; }
        var remaining = root.scrollHeight - (y + window.innerHeight);
        nav.hidden = !enough(entries.filter(function (e) { return !e.li.hidden; }).length);
        var items = entries.filter(function (e) { return !e.li.hidden; }).map(function (e) {
          return { id: e.id, top: target(e).getBoundingClientRect().top };
        });
        mark(pick(items, window.innerHeight, remaining, y));
      } catch (e) { /* the links still work */ }
    }

    function schedule() {
      if (queued) return;
      queued = true;
      (window.requestAnimationFrame || setTimeout)(update);
    }

    if ('IntersectionObserver' in window) {
      // A thin band at 30% of the viewport: a section's top crossing it is the moment to change.
      io = new IntersectionObserver(schedule, { rootMargin: '-30% 0px -69% 0px', threshold: [0, 1] });
    }
    if ('MutationObserver' in window) mo = new MutationObserver(schedule);
    collect();
    window.addEventListener('scroll', schedule, { passive: true });
    window.addEventListener('resize', schedule);
    document.body.addEventListener('htmx:afterSwap', function () { collect(); schedule(); });
    nav.addEventListener('click', function (ev) {
      var a = ev.target && ev.target.closest ? ev.target.closest('a[href^="#"]') : null;
      if (!a) return;
      var id = decodeURIComponent(a.getAttribute('href').slice(1)), t = document.getElementById(id);
      mark(id);
      if (!t) return;
      var root = document.documentElement, y = window.pageYOffset || root.scrollTop || 0;
      var margin = parseFloat(window.getComputedStyle(t).scrollMarginTop) || 0;
      var dest = Math.max(0, Math.min(t.getBoundingClientRect().top + y - margin, root.scrollHeight - window.innerHeight));
      pin = { id: id, dest: dest, settled: false };
    });
    // Scrolling by hand lets go of a clicked entry.
    var release = function () { pin = null; schedule(); };
    document.addEventListener('pointerdown', function (ev) {
      var onLink = !!(ev.target && ev.target.closest && ev.target.closest('nav.toc a'));
      pin = afterPointerDown(pin, onLink);
      if (!onLink) schedule();
    }, { passive: true });
    window.addEventListener('wheel', release, { passive: true });
    window.addEventListener('touchstart', release, { passive: true });
    window.addEventListener('keydown', function (ev) {
      if (['ArrowUp', 'ArrowDown', 'PageUp', 'PageDown', 'Home', 'End', ' '].indexOf(ev.key) >= 0) release();
    });
    update();
  } catch (e) { /* never break the page */ }
})();
