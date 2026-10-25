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

  /* ---------- hover, focus and click: what to do next ---------- */

  var OPEN_AFTER = 120, CLOSE_AFTER = 200;

  // state: {open, pinned}. ev: 'enter' | 'leave' (the pointer on the button or the popover), 'timer-open',
  // 'timer-close', 'click', 'focus', 'blur', 'closed' (the browser closed it: Escape or a click away).
  // Returns {open, pinned, act} where act is 'open' | 'close' | 'schedule-open' | 'schedule-close' | 'cancel' | null.
  // Hovering or focusing opens it; a click pins it open; a pinned one stays until a click away, Escape or another click.
  function next(state, ev) {
    var o = !!(state && state.open), p = !!(state && state.pinned);
    var out = function (open, pinned, act) { return { open: open, pinned: pinned, act: act }; };
    switch (ev) {
      case 'enter': return o ? out(o, p, 'cancel') : out(o, p, 'schedule-open');
      case 'leave': return !o ? out(o, p, 'cancel') : p ? out(o, p, null) : out(o, p, 'schedule-close');
      case 'timer-open': return o ? out(o, p, null) : out(true, false, 'open');
      case 'timer-close': return o && !p ? out(false, false, 'close') : out(o, p, null);
      case 'click': return o && p ? out(false, false, 'close') : o ? out(true, true, 'cancel') : out(true, true, 'open');
      case 'focus': return o ? out(o, p, null) : out(true, false, 'open');
      case 'blur': return o && !p ? out(false, false, 'close') : out(o, p, null);
      case 'closed': return out(false, false, 'cancel');
      default: return out(o, p, null);
    }
  }

  var api = { place: place, next: next };
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

  function hovers() {
    return !!(window.matchMedia && window.matchMedia('(hover: hover) and (pointer: fine)').matches) && !small();
  }

  // One small state machine per popover, driven by next(); the timers are cleared whenever it settles.
  var machines = {};
  function machine(pop, btn) {
    var m = { state: { open: false, pinned: false }, openT: null, closeT: null };
    function clear() { clearTimeout(m.openT); clearTimeout(m.closeT); m.openT = m.closeT = null; }
    function send(ev) {
      try {
        var r = next(m.state, ev);
        m.state = { open: r.open, pinned: r.pinned };
        if (r.act === 'cancel') clear();
        else if (r.act === 'schedule-open') { clear(); m.openT = setTimeout(function () { send('timer-open'); }, OPEN_AFTER); }
        else if (r.act === 'schedule-close') { clear(); m.closeT = setTimeout(function () { send('timer-close'); }, CLOSE_AFTER); }
        else if (r.act === 'open') { clear(); if (!pop.matches(':popover-open')) pop.showPopover(); }
        else if (r.act === 'close') { clear(); if (pop.matches(':popover-open')) pop.hidePopover(); }
      } catch (e) { /* the native button still works */ }
    }
    m.send = send;
    var enter = function () { if (hovers()) send('enter'); }, leave = function () { if (hovers()) send('leave'); };
    btn.addEventListener('mouseenter', enter); btn.addEventListener('mouseleave', leave);
    pop.addEventListener('mouseenter', enter); pop.addEventListener('mouseleave', leave);
    btn.addEventListener('focus', function () { if (hovers() && btn.matches(':focus-visible')) send('focus'); });
    btn.addEventListener('blur', function () { if (hovers()) send('blur'); });
    btn.addEventListener('click', function (ev) {
      if (!hovers()) return;  // touch and small screens keep the native tap to open
      ev.preventDefault();    // the native toggle would close a popover that hover opened
      send('click');
    });
    return m;
  }

  function bind() {
    try {
      var btns = document.querySelectorAll('.info[popovertarget]');
      for (var i = 0; i < btns.length; i++) {
        var pop = document.getElementById(btns[i].getAttribute('popovertarget'));
        if (pop && !machines[pop.id] && pop.showPopover) machines[pop.id] = machine(pop, btns[i]);
      }
    } catch (e) { /* ignore */ }
  }
  bind();
  document.addEventListener('htmx:afterSwap', bind);

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
        if (machines[pop.id]) machines[pop.id].send('closed');
      }
    } catch (e) { /* ignore */ }
  }, true);
})();
