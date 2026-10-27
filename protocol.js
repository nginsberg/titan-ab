// Titan ACE (Titan Drills Pro) BLE protocol helpers. Pure byte work, no Bluetooth.
// Reconstructed from static analysis of the Android app (blutter output), NOT yet verified on hardware.
// Loaded as a plain <script> in the browser (window.TitanProtocol) and via require() in node tests.
(function (root) {
  'use strict';

  var FUNC = { CONTROL: 0x01, SET_CURRENT: 0x02, EXEC_MANUAL: 0x03, SAVE_DRILL: 0x08,
               STATUS: 0x17, VERSION: 0x18, VERIFY: 0x20 };
  var CONTROL = { START: 0, PAUSE: 1, STOP: 2 };
  // protocol/response/get_robot_status_response.dart: byte 3 indexes this list
  var STATUS_NAMES = ['START', 'PAUSE', 'STOP', 'INIT', 'PENDING_START', 'MOTO_STARTING',
                      'MOTO_STOPPING', 'ALARM', 'TESTING', 'TEST_PAUSED'];

  // CRC-16/XMODEM, poly 0x1021, init 0 (common/xmodem_crc.dart)
  function crc16(bytes) {
    var crc = 0;
    for (var i = 0; i < bytes.length; i++) {
      crc ^= (bytes[i] & 0xff) << 8;
      for (var b = 0; b < 8; b++) crc = (crc & 0x8000) ? ((crc << 1) ^ 0x1021) & 0xffff : (crc << 1) & 0xffff;
    }
    return crc;
  }

  // raw = [func][lenHi][lenLo][payload][crcHi][crcLo]
  function rawFrame(func, payload) {
    payload = payload ? Array.prototype.slice.call(payload) : [];
    var body = [func & 0xff, (payload.length >> 8) & 0xff, payload.length & 0xff].concat(payload);
    var c = crc16(body);
    body.push((c >> 8) & 0xff, c & 0xff);
    return body;
  }

  // protocol/base/bluetooth_communication.dart _escapeBytes: 7E -> 7D 5E, 7D -> 7D 5D
  function escapeBytes(bytes) {
    var out = [];
    for (var i = 0; i < bytes.length; i++) {
      var b = bytes[i];
      if (b === 0x7e) out.push(0x7d, 0x5e);
      else if (b === 0x7d) out.push(0x7d, 0x5d);
      else out.push(b);
    }
    return out;
  }

  // On the wire: leading 0x7E, then escaped raw frame.
  function buildFrame(func, payload) {
    return Uint8Array.from([0x7e].concat(escapeBytes(rawFrame(func, payload))));
  }

  // The app writes 20-byte chunks, sequentially.
  function chunk(bytes, n) {
    n = n || 20;
    var out = [];
    for (var i = 0; i < bytes.length; i += n) out.push(bytes.slice(i, i + n));
    return out;
  }

  // Incremental parser, modelled on the app's responseProcess (logic/ble_titan_drills_logic.dart):
  // - 0x7E (re)starts a frame; "7D xx" un-escapes to xx ^ 0x20 (so 7D 5E -> 7E, 7D 5D -> 7D).
  // - After the 3 header bytes [func][lenHi][lenLo], a frame with len 0 is dispatched immediately, with NO CRC check
  //   (crcOk is reported true, noCrc true). Longer frames complete after 5 + len bytes and are CRC-checked.
  // - A reply may arrive without a leading 0x7E; that is accepted too.
  // This is only for replies. Requests the page sends always carry their CRC (e.g. 7e 18 00 00 ea c2).
  function FrameParser(onFrame) {
    var buf = [], esc = false, trail = null;
    function reset() { buf = []; esc = false; trail = null; }
    this.reset = reset;
    this.feed = function (bytes) {
      for (var i = 0; i < bytes.length; i++) {
        var b = bytes[i] & 0xff;
        if (b === 0x7e) { reset(); continue; }
        if (esc) { esc = false; b = (b ^ 0x20) & 0xff; }
        else if (b === 0x7d) { esc = true; continue; }
        // After a zero-length reply the machine may still append the usual 2 CRC bytes; swallow them if they match.
        if (trail) {
          if (b === trail.crc[trail.i]) { if (++trail.i === 2) trail = null; continue; }
          trail = null;
        }
        buf.push(b);
        if (buf.length >= 3) {
          var len = (buf[1] << 8) | buf[2];
          if (len === 0) {
            var h = buf.slice(0, 3), c = crc16(h);
            reset(); trail = { crc: [(c >> 8) & 0xff, c & 0xff], i: 0 };
            onFrame({ func: h[0], payload: [], raw: h, crcOk: true, noCrc: true });
          } else if (buf.length >= 5 + len) {
            var need = 5 + len, raw = buf.slice(0, need);
            reset();
            var crcOk = crc16(raw.slice(0, need - 2)) === ((raw[need - 2] << 8) | raw[need - 1]);
            onFrame({ func: raw[0], payload: raw.slice(3, need - 2), raw: raw, crcOk: crcOk });
          }
        }
      }
    };
  }

  // ---- levels -> labels (common/level_mapping_utils.dart; pickleball tables) ----
  var HORZ_DEG = [11, 10.5, 10, 9, 8, 7, 5, 3.5, 2, 0, 2, 3.5, 5, 7, 8, 9, 10, 10.5, 11];
  var SPEED_MPH = [5, 9, 13, 17, 21, 25, 29, 32, 35, 38, 41, 44, 47, 50, 54, 57, 60, 64, 67, 70];
  var FEED_SEC = [1, 1.5, 2, 2.5, 3, 3.5, 4, 4.5, 5, 5.5, 6, 6.5, 7, 7.5, 8, 8.5, 9, 9.5, 15, 20];
  var HEIGHT_FIRST = [6, 8, 10, 12, 14, 16];
  function heightDeg(level) { var i = level - 1; return i < HEIGHT_FIRST.length ? HEIGHT_FIRST[i] : 11 + i; }

  var RANGES = { horz: [1, 19], speed: [1, 20], spin: [1, 19], height: [1, 50], feed: [1, 20] };
  var FIELDS = ['horz', 'speed', 'spin', 'height', 'feed'];

  function label(field, v) {
    switch (field) {
      case 'horz': return v === 10 ? 'straight' : HORZ_DEG[v - 1] + '° ' + (v < 10 ? 'left' : 'right');
      case 'speed': return SPEED_MPH[v - 1] + ' mph';
      case 'spin': return v === 10 ? 'no spin' : Math.abs(v - 10) * 300 + ' rpm ' + (v < 10 ? 'backspin' : 'topspin');
      case 'height': return heightDeg(v) + '°';
      case 'feed': return FEED_SEC[v - 1] + ' s';
    }
    return String(v);
  }

  function clampShot(s) {
    var out = {};
    FIELDS.forEach(function (f) {
      var v = Math.round(Number(s[f]));
      if (!isFinite(v)) v = RANGES[f][0];
      out[f] = Math.min(RANGES[f][1], Math.max(RANGES[f][0], v));
    });
    return out;
  }

  // ---- drill encoding (request/save_drill_request.dart, model/ball.dart, model/myshot.dart) ----
  // Shot = [horz][speed][spin][height][feed]
  function shotBytes(s) { s = clampShot(s); return FIELDS.map(function (f) { return s[f]; }); }
  // Ball = [type 0 Regular / 1 Random][loop][nShots] + shots
  function ballBytes(random, loop, shots) {
    var out = [random ? 1 : 0, loop, shots.length];
    shots.forEach(function (s) { out = out.concat(shotBytes(s)); });
    return out;
  }
  // SaveDrill payload = [drillNo][nameLen][name GBK][machinePos][shuffle][nBalls] + balls. Name is ASCII here (GBK == ASCII).
  function saveDrillPayload(o) {
    var name = [];
    for (var i = 0; i < o.name.length; i++) {
      var c = o.name.charCodeAt(i);
      if (c > 0x7e) throw new Error('drill name must be ASCII');
      name.push(c);
    }
    var out = [o.number, name.length].concat(name, [o.machinePos || 0, o.shuffle ? 1 : 0, o.balls.length]);
    o.balls.forEach(function (b) { out = out.concat(b); });
    return out;
  }
  // "pairs" x (Random ball from A, Random ball from B), loop 1 each.
  function abBalls(A, B, pairs) {
    var balls = [];
    for (var i = 0; i < pairs; i++) { balls.push(ballBytes(true, 1, A)); balls.push(ballBytes(true, 1, B)); }
    return balls;
  }

  var requests = {
    getVersion: function () { return buildFrame(FUNC.VERSION, null); },
    getStatus: function () { return buildFrame(FUNC.STATUS, null); },
    control: function (cmd) { return buildFrame(FUNC.CONTROL, [CONTROL[cmd]]); },
    setCurrent: function (nums) { return buildFrame(FUNC.SET_CURRENT, nums); },
    execManual: function (param, value) { return buildFrame(FUNC.EXEC_MANUAL, [param, value]); },
    saveDrill: function (o) { return buildFrame(FUNC.SAVE_DRILL, saveDrillPayload(o)); },
    // protocol/request/verify_code_request.dart: 9 bytes [kind][hasCode][6 ASCII digits][0].
    // kind 0 = automatic check right after the version reply (stored code or none), 1 = code typed by the user.
    verify: function (kind, code) {
      var p = [kind, code ? 1 : 0, 0, 0, 0, 0, 0, 0, 0];
      if (code) for (var i = 0; i < 6 && i < code.length; i++) p[2 + i] = code.charCodeAt(i);
      return buildFrame(FUNC.VERIFY, p);
    }
  };
  // Mega Drill: same slot repeated N times; one entry if N == 1.
  function repeatSlots(slot, n) { var a = []; for (var i = 0; i < Math.max(1, n); i++) a.push(slot); return a; }

  // protocol/response/get_version_response.dart: ASCII of raw[3..17); char 5 = major, 7 = minor, 9 = patch, 10 = mode letter (P/Q pickle, D tennis).
  function parseVersion(raw) {
    var s = '';
    for (var i = 3; i < 17 && i < raw.length; i++) s += String.fromCharCode(raw[i]);
    var d = function (k) { var n = parseInt(s.charAt(k), 10); return isNaN(n) ? null : n; };
    var v = { text: s, major: d(5), minor: d(7), patch: d(9), mode: s.charAt(10) };
    // get_version_response.dart: D -> tennis, P and Q -> pickle (Q also sets PICKLE_Q), anything else -> tennis.
    v.modeName = (v.mode === 'P' || v.mode === 'Q') ? 'pickle' : 'tennis';
    // pages/linking: the app sends the automatic VerifyCode only when the mode equals the app's mode (pickle for this
    // page) and major >= 7 and minor >= 4 (taken literally).
    v.needsPin = v.modeName === 'pickle' && v.major !== null && v.minor !== null && v.major >= 7 && v.minor >= 4;
    return v;
  }
  // verify_code_response.dart: resp byte 3: 0 ok, 1 failed, 2 needs PIN
  function parseVerify(resp) {
    var c = resp.payload[0];
    return c === 0 ? 'ok' : c === 2 ? 'needPin' : 'failed';
  }
  function parseStatus(resp) {
    var c = resp.payload[0];
    return { code: c, name: c < STATUS_NAMES.length ? STATUS_NAMES[c] : 'UNKNOWN(' + c + ')', extra: resp.payload.slice(1) };
  }

  function hex(bytes) {
    return Array.prototype.map.call(bytes, function (b) { return (b < 16 ? '0' : '') + b.toString(16); }).join(' ');
  }

  var api = {
    FUNC: FUNC, CONTROL: CONTROL, STATUS_NAMES: STATUS_NAMES, RANGES: RANGES, FIELDS: FIELDS,
    crc16: crc16, rawFrame: rawFrame, escapeBytes: escapeBytes, buildFrame: buildFrame, chunk: chunk,
    FrameParser: FrameParser, label: label, clampShot: clampShot, shotBytes: shotBytes, ballBytes: ballBytes,
    saveDrillPayload: saveDrillPayload, abBalls: abBalls, requests: requests, repeatSlots: repeatSlots,
    parseVersion: parseVersion, parseVerify: parseVerify, parseStatus: parseStatus, hex: hex
  };
  if (typeof module !== 'undefined' && module.exports) module.exports = api; else root.TitanProtocol = api;
})(this);
