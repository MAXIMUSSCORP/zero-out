/* Anchors each ? popover to the button that opened it. The browser centers a popover in the
   window by default, which can put it far from its button; this puts it just below the button,
   or above when there is no room. On a small screen the stylesheet makes it a bottom sheet and
   this file does nothing. Without this file a popover still opens, centered. */
(function () {
  'use strict';

  /* ---------- where the popover goes ---------- */

  // btn: the button's rect {top, bottom, left}; size: {w, h} of the popover; view: {w, h} of the window.
  // Below the button, left aligned with it; above when it does not fit below and does fit above;
  // never closer than margin to a window edge.
  function place(btn, size, view, gap, margin) {
    gap = gap === undefined ? 8 : gap;
    margin = margin === undefined ? 16 : margin;
    var left = Math.max(margin, Math.min(btn.left, view.w - size.w - margin));
    var top = btn.bottom + gap;
    if (top + size.h > view.h - margin && btn.top - gap - size.h >= margin) top = btn.top - gap - size.h;
    top = Math.max(margin, Math.min(top, view.h - size.h - margin));
    return { top: top, left: left };
  }

  var api = { place: place };
  if (typeof module !== 'undefined') module.exports = api;
  if (typeof document === 'undefined') return;

  /* ---------- page ---------- */

  var open = null;  // {pop, btn} while a popover is showing

  function small() {
    return !!(window.matchMedia && window.matchMedia('(max-width: 734px)').matches);
  }

  function invoker(pop) {
    var all = document.querySelectorAll('[popovertarget]');
    for (var i = 0; i < all.length; i++) if (all[i].getAttribute('popovertarget') === pop.id) return all[i];
    return null;
  }

  function reposition() {
    try {
      if (!open) return;
      var s = open.pop.style;
      s.left = '0px'; s.top = '0px';  // measure at full width, not squeezed against the right edge
      var r = open.pop.getBoundingClientRect(), b = open.btn.getBoundingClientRect();
      var at = place(b, { w: r.width, h: r.height }, { w: window.innerWidth, h: window.innerHeight });
      s.left = at.left + 'px'; s.top = at.top + 'px';
      s.visibility = '';
    } catch (e) { /* the popover stays where the browser put it */ }
  }

  function clear(pop) {
    ['position', 'margin', 'inset', 'left', 'top', 'visibility'].forEach(function (p) { pop.style[p] = ''; });
  }

  // beforetoggle hides the popover until it is placed, so it never flashes in the middle of the window.
  document.addEventListener('beforetoggle', function (ev) {
    try {
      var pop = ev.target;
      if (!pop.classList || !pop.classList.contains('pop') || ev.newState !== 'open' || small()) return;
      if (!invoker(pop)) return;
      pop.style.position = 'fixed'; pop.style.margin = '0'; pop.style.inset = 'auto'; pop.style.visibility = 'hidden';
    } catch (e) { /* ignore */ }
  }, true);

  document.addEventListener('toggle', function (ev) {
    try {
      var pop = ev.target;
      if (!pop.classList || !pop.classList.contains('pop')) return;
      if (ev.newState === 'open') {
        var btn = invoker(pop);
        if (small() || !btn) { clear(pop); return; }
        open = { pop: pop, btn: btn };
        reposition();
        window.addEventListener('scroll', reposition, { passive: true });
        window.addEventListener('resize', reposition, { passive: true });
      } else {
        window.removeEventListener('scroll', reposition);
        window.removeEventListener('resize', reposition);
        if (open && open.pop === pop) open = null;
        clear(pop);
      }
    } catch (e) { /* ignore */ }
  }, true);
})();
