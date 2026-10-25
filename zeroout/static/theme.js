// Light, Dark or Match system: a per-browser choice, kept in localStorage.
// Loaded in <head> so the page never flashes the wrong theme; storage can be blocked, so every access is guarded.
(function () {
  'use strict';
  var KEY = 'zeroout.theme';

  function get() {
    try { return localStorage.getItem(KEY) || 'system'; } catch (e) { return 'system'; }
  }

  function apply(choice) {
    var root = document.documentElement;
    if (choice === 'light' || choice === 'dark') root.setAttribute('data-theme', choice);
    else root.removeAttribute('data-theme');
    var buttons = document.querySelectorAll('[data-theme-set]');
    for (var i = 0; i < buttons.length; i++) {
      buttons[i].setAttribute('aria-pressed', String(buttons[i].getAttribute('data-theme-set') === choice));
    }
  }

  apply(get());
  document.addEventListener('DOMContentLoaded', function () { apply(get()); });
  document.addEventListener('htmx:afterSwap', function () { apply(get()); });
  document.addEventListener('click', function (ev) {
    var b = ev.target.closest ? ev.target.closest('[data-theme-set]') : null;
    if (!b) return;
    var choice = b.getAttribute('data-theme-set');
    try {
      if (choice === 'system') localStorage.removeItem(KEY); else localStorage.setItem(KEY, choice);
    } catch (e) { /* storage blocked: the choice lasts until the page is left */ }
    apply(choice);
  });
})();
