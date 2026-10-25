// Phone in hand: records the menu through the PC microphone, uploads it, shows what was heard.
// Everything from the server is placed with textContent; labels come from strangers' phone menus.
(function () {
  'use strict';
  var TARGET_RATE = 16000;
  var FRAME_SECONDS = 0.05;
  var FLOOR_WINDOW_SECONDS = 3;
  var SILENCE_SECONDS = 5;
  var MAX_SECONDS = 90;
  var MIN_SPEECH = 0.01;
  var MAX_FLOOR = 0.02;

  /* ---------- pure helpers ---------- */

  function downsample(samples, fromRate, toRate) {
    if (toRate >= fromRate) return Float32Array.from(samples);
    var ratio = fromRate / toRate;
    var n = Math.floor(samples.length / ratio);
    var out = new Float32Array(n);
    for (var i = 0; i < n; i++) {
      var start = Math.round(i * ratio);
      var end = Math.min(samples.length, Math.round((i + 1) * ratio));
      if (end <= start) end = start + 1;
      var sum = 0;
      for (var j = start; j < end; j++) sum += samples[j];
      out[i] = sum / (end - start);
    }
    return out;
  }

  function encodeWav(samples, rate) {
    var buf = new ArrayBuffer(44 + samples.length * 2);
    var v = new DataView(buf);
    function str(off, s) { for (var i = 0; i < s.length; i++) v.setUint8(off + i, s.charCodeAt(i)); }
    str(0, 'RIFF');
    v.setUint32(4, 36 + samples.length * 2, true);
    str(8, 'WAVE');
    str(12, 'fmt ');
    v.setUint32(16, 16, true);
    v.setUint16(20, 1, true);
    v.setUint16(22, 1, true);
    v.setUint32(24, rate, true);
    v.setUint32(28, rate * 2, true);
    v.setUint16(32, 2, true);
    v.setUint16(34, 16, true);
    str(36, 'data');
    v.setUint32(40, samples.length * 2, true);
    for (var i = 0; i < samples.length; i++) {
      var s = Math.max(-1, Math.min(1, samples[i]));
      v.setInt16(44 + i * 2, s < 0 ? Math.round(s * 32768) : Math.round(s * 32767), true);
    }
    return buf;
  }

  function rms(frame) {
    if (!frame.length) return 0;
    var sum = 0;
    for (var i = 0; i < frame.length; i++) sum += frame[i] * frame[i];
    return Math.sqrt(sum / frame.length);
  }

  // Tracks speech and silence frame by frame. The noise floor is the 10th percentile of the last 3 s,
  // so it follows the room even when recording starts while the menu is already speaking.
  // Sound is any frame over MIN_SPEECH. A frame is quiet only when it is under MAX_FLOOR and under
  // max(floor * 3, MIN_SPEECH), so steady music or a long prompt never counts as silence.
  // push(level) returns true when recording should stop; capped and sound say why and what was heard.
  function makeEndpointer(frameSeconds) {
    // Counted in frames, so 5 s is exactly 100 frames of 50 ms, free of float drift.
    var windowFrames = Math.round(FLOOR_WINDOW_SECONDS / frameSeconds);
    var silenceFrames = Math.round(SILENCE_SECONDS / frameSeconds);
    var maxFrames = Math.round(MAX_SECONDS / frameSeconds);
    var recent = [], n = 0, quiet = 0;
    var ep = { capped: false, sound: false };
    ep.push = function (level) {
      n += 1;
      recent.push(level);
      if (recent.length > windowFrames) recent.shift();
      var sorted = recent.slice().sort(function (a, b) { return a - b; });
      var floor = sorted[Math.floor(0.1 * (sorted.length - 1))];
      if (level > MIN_SPEECH) ep.sound = true;
      // Quiet means near the room's floor and under the absolute ceiling; steady sound is never quiet.
      if (level < MAX_FLOOR && level <= Math.max(floor * 3, MIN_SPEECH)) quiet += 1;
      else quiet = 0;
      if (n >= maxFrames) { ep.capped = true; return true; }
      return ep.sound && quiet >= silenceFrames;
    };
    return ep;
  }

  var EXPIRED_TEXT = 'This Zero-Out session has ended. Open it again from the link that zeroout run printed.';
  var MIC_TEXT = 'Zero-Out needs the microphone to hear the menu. Allow it in the browser and try again.';

  function nextText(next, skipped) {
    if (!next) {
      return skipped > 0
        ? 'Every option Zero-Out could reach is mapped or was skipped. Hang up.'
        : 'This menu is mapped as far as Zero-Out goes. Hang up.';
    }
    if (next.press_now) {
      return 'Press ' + next.press_now + ' now (' + next.label + '). Then click Start listening.';
    }
    if (!next.keys || !next.keys.length) return 'Call the number. Click Start listening when it answers.';
    return 'Hang up and call again. Press ' + next.keys.join(', then ') +
      ', waiting for each menu to finish. Click Start listening right after your last key.';
  }

  // What the page shows for a step: the key to light, the headline, and the heard lines as plain data.
  function litKey(step) {
    if (!step) return '';
    if (step.press_now) return String(step.press_now);
    return step.keys && step.keys.length ? String(step.keys[0]) : '';
  }

  function headlineText(next) {
    if (!next) return 'Hang up';
    if (next.press_now) return 'Press ' + next.press_now + ' now';
    if (!next.keys || !next.keys.length) return 'Call the number';
    return 'Call again, press ' + next.keys.join(', then ');
  }

  // heard.lines when the server sent them, else the whole prompt as one line.
  function heardLines(text, lines) {
    if (Array.isArray(lines) && lines.length) return lines;
    return text ? [{ text: text, key: null }] : [];
  }

  // Fills a list with one li per line; a keyed line starts with a key cap. textContent only.
  function renderLines(doc, list, lines) {
    list.textContent = '';
    (lines || []).forEach(function (ln) {
      var li = doc.createElement('li');
      if (ln.key) {
        var cap = doc.createElement('span');
        cap.className = 'key sm';
        cap.textContent = ln.key;
        li.appendChild(cap);
        var sep = doc.createElement('span');
        sep.className = 'vh';
        sep.textContent = ': ';
        li.appendChild(sep);
      }
      var t = doc.createElement('span');
      t.className = 'text';
      t.textContent = ln.text;
      li.appendChild(t);
      list.appendChild(li);
    });
  }

  // The state transitions of one listen cycle, apart from the DOM so node can test them.
  // io: record(), upload(path, rec), decide(), send(name, payload), ui: {busy, error, status, heard, next, review}.
  function makeController(io) {
    var ui = io.ui;
    var ctl = { current: null, busy: false };

    function setBusy(on) { ctl.busy = on; ui.busy(on); }

    ctl.run = async function () {
      if (ctl.busy) return;
      setBusy(true);
      ui.error('');
      var path = ctl.current ? ctl.current.path : '';  // the option this clip is for, fixed at the click
      try {
        var rec;
        try {
          rec = await io.record();
        } catch (e) {
          ui.error(e && (e.name === 'NotAllowedError' || e.name === 'SecurityError')
            ? MIC_TEXT : 'Zero-Out could not start recording in this browser.');
          return;
        }
        ui.status('Hearing that…');
        var heard = await io.upload(path, rec);
        ui.status('');
        if (!heard.heard) {
          ui.heard(heard.message, '', []);
          ctl.current = heard.next || null;
          ui.next(ctl.current, heard.skipped);
          return;
        }
        ui.heard(heard.message, heard.heard.prompt_text, heard.heard.options);
        ui.review(true);
        var choice = await io.decide();
        ui.review(false);
        var out = await io.send(choice, { nonce: heard.nonce });
        ui.heard(out.message, '', choice === 'keep' ? out.options : []);
        ctl.current = out.next || null;
        ui.next(ctl.current, out.skipped);
      } catch (e) {
        ui.status('');
        ui.review(false);
        ui.error(e && e.message ? e.message : 'Something went wrong hearing that clip.');
      } finally {
        ui.status('');
        setBusy(false);
      }
    };

    ctl.skip = async function () {
      if (ctl.busy || !ctl.current || !ctl.current.path) return;
      setBusy(true);
      ui.error('');
      try {
        var out = await io.send('skip', { path: ctl.current.path });
        ui.heard(out.message, '', []);
        ctl.current = out.next || null;
        ui.next(ctl.current, out.skipped);
      } catch (e) {
        ui.error(e && e.message ? e.message : 'Something went wrong.');
      } finally {
        setBusy(false);
      }
    };

    return ctl;
  }

  var api = {
    downsample: downsample, encodeWav: encodeWav, rms: rms, makeEndpointer: makeEndpointer,
    nextText: nextText, litKey: litKey, headlineText: headlineText,
    heardLines: heardLines, renderLines: renderLines, makeController: makeController, EXPIRED_TEXT: EXPIRED_TEXT,
  };
  if (typeof window !== 'undefined') window.ZeroOutPhone = api;
  if (typeof module !== 'undefined') module.exports = api;
  if (typeof document === 'undefined' || !document.getElementById('start')) return;

  /* ---------- page ---------- */

  var $ = function (id) { return document.getElementById(id); };
  var nextEl = $('next'), startBtn = $('start'), stopBtn = $('stop'), skipBtn = $('skip');
  var statusEl = $('status'), meter = $('meter'), errorEl = $('error');
  var reviewEl = $('review'), keepBtn = $('keep'), discardBtn = $('discard');
  var cid = nextEl.dataset.company;
  var base = '/c/' + cid + '/phone';
  var stopRecording = null;

  function showError(text) {
    errorEl.textContent = text;
    errorEl.hidden = !text;
  }

  var lastLines = null;  // heard.lines of the latest clip, kept by upload() for showHeard

  function lightKeypad(step) {
    var lit = litKey(step);
    Array.prototype.forEach.call(document.querySelectorAll('#keypad .key'), function (k) {
      k.classList.toggle('lit', lit !== '' && k.dataset.key === lit);
    });
  }

  function showHeard(message, text, options) {
    $('message').textContent = message || '';
    var list = $('heard-text');
    renderLines(document, list, heardLines(text, text ? lastLines : null));
    list.hidden = !text;
    renderLines(document, $('options'), (options || []).map(function (o) {
      return { key: o.key, text: o.label };
    }));
    $('heard').hidden = !(message || text || (options && options.length));
  }

  async function post(url, body, type) {
    var r = await fetch(url, { method: 'POST', body: body, headers: { 'Content-Type': type } });
    var data = null;
    try { data = await r.json(); } catch (e) { data = null; }
    if (!r.ok || !data) {
      if (!data && r.status === 403) throw new Error(EXPIRED_TEXT);  // the guard's HTML page
      throw new Error(data && data.error ? data.error : 'Something went wrong hearing that clip.');
    }
    return data;
  }

  // Records until the endpointer or the Stop button ends it; always releases the microphone.
  async function record() {
    var stream = await navigator.mediaDevices.getUserMedia({
      audio: { echoCancellation: false, noiseSuppression: false, autoGainControl: true }
    });
    var ctx = null, node = null;
    try {
      ctx = new (window.AudioContext || window.webkitAudioContext)();
      await ctx.audioWorklet.addModule('/static/recorder-worklet.js');
      var src = ctx.createMediaStreamSource(stream);
      node = new AudioWorkletNode(ctx, 'recorder');
      src.connect(node);
      var chunks = [], frame = [], frameLen = 0;
      var frameSize = Math.round(ctx.sampleRate * FRAME_SECONDS);
      var ep = makeEndpointer(FRAME_SECONDS);
      statusEl.textContent = 'Listening…';
      meter.hidden = false;
      stopBtn.hidden = false;
      await new Promise(function (resolve) {
        stopRecording = resolve;
        node.port.onmessage = function (ev) {
          chunks.push(ev.data);
          frame.push(ev.data);
          frameLen += ev.data.length;
          if (frameLen < frameSize) return;
          var all = new Float32Array(frameLen), off = 0;
          frame.forEach(function (c) { all.set(c, off); off += c.length; });
          frame = []; frameLen = 0;
          var level = rms(all);
          meter.firstElementChild.style.width = Math.min(100, Math.round(level * 400)) + '%';
          if (ep.push(level)) resolve();
        };
      });
      var total = chunks.reduce(function (n, c) { return n + c.length; }, 0);
      var samples = new Float32Array(total), at = 0;
      chunks.forEach(function (c) { samples.set(c, at); at += c.length; });
      return { samples: samples, rate: ctx.sampleRate, capped: ep.capped, sound: ep.sound };
    } finally {
      stopRecording = null;
      if (node) { node.port.onmessage = null; node.disconnect(); }
      stream.getTracks().forEach(function (t) { t.stop(); });
      if (ctx) ctx.close();
      meter.hidden = true;
      stopBtn.hidden = true;
    }
  }

  function decide() {
    return new Promise(function (resolve) {
      keepBtn.onclick = function () { resolve('keep'); };
      discardBtn.onclick = function () { resolve('discard'); };
    });
  }

  var ctl = makeController({
    record: record,
    upload: function (path, rec) {
      var wav = encodeWav(downsample(rec.samples, rec.rate, TARGET_RATE), Math.min(rec.rate, TARGET_RATE));
      var q = '?path=' + encodeURIComponent(path) + '&capped=' + (rec.capped ? 1 : 0) + '&sound=' + (rec.sound ? 1 : 0);
      return post(base + '/clip' + q, wav, 'audio/wav').then(function (data) {
        lastLines = data.heard && data.heard.lines ? data.heard.lines : null;
        return data;
      });
    },
    decide: decide,
    send: function (name, payload) { return post(base + '/' + name, JSON.stringify(payload), 'application/json'); },
    ui: {
      busy: function (on) { startBtn.disabled = on; skipBtn.disabled = on; },
      error: showError,
      status: function (t) { statusEl.textContent = t; },
      heard: showHeard,
      next: function (step, skipped) {
        nextEl.textContent = nextText(step, skipped);
        $('headline').textContent = headlineText(step);
        lightKeypad(step);
        skipBtn.hidden = !step || !step.path;
      },
      review: function (on) { reviewEl.hidden = !on; },
    },
  });
  ctl.current = nextEl.dataset.hasStep === '1'
    ? { path: nextEl.dataset.path, keys: nextEl.dataset.keys ? nextEl.dataset.keys.split(',') : [],
        press_now: nextEl.dataset.pressNow || null, label: nextEl.dataset.label }
    : null;

  startBtn.addEventListener('click', ctl.run);
  stopBtn.addEventListener('click', function () { if (stopRecording) stopRecording(); });
  skipBtn.addEventListener('click', ctl.skip);
  window.addEventListener('pagehide', function () { if (stopRecording) stopRecording(); });
})();
