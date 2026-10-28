// node test.js : protocol checks, no dependencies, exits non-zero on failure.
'use strict';
const P = require('./protocol.js');
const { execFileSync } = require('child_process');
// Optional cross-check against a local reference encoder; skipped when TITAN_FRAMES_PY is unset.
const SCRIPT = process.env.TITAN_FRAMES_PY;

let failed = 0, passed = 0;
function t(name, fn) {
  try { fn(); passed++; console.log('ok   ' + name); }
  catch (e) { failed++; console.log('FAIL ' + name + '\n     ' + e.message); }
}
function eq(a, b, msg) {
  const x = typeof a === 'string' ? a : JSON.stringify(Array.from(a)), y = typeof b === 'string' ? b : JSON.stringify(Array.from(b));
  if (x !== y) throw new Error((msg || 'mismatch') + '\n     got      ' + x + '\n     expected ' + y);
}
const hex = P.hex;
const unhex = (s) => s.trim().split(/\s+/).map((h) => parseInt(h, 16));

// ---- known vectors (math verified in analysis) ----
t('vector GetVersion', () => eq(hex(P.requests.getVersion()), '7e 18 00 00 ea c2'));
t('vector Control START', () => eq(hex(P.requests.control('START')), '7e 01 00 01 00 45 85'));
t('vector ExecManual speed 7', () => eq(hex(P.requests.execManual(1, 7)), '7e 03 00 02 01 07 c3 64'));
t('crc16 xmodem check string', () => eq(P.crc16(Array.from('123456789', (c) => c.charCodeAt(0))).toString(16), '31c3'));

// ---- escaping ----
t('payload 7E/7D escaped', () => {
  // body = 02 00 02 7e 7d crcHi crcLo ; escaped 7e->7d 5e, 7d->7d 5d
  const f = Array.from(P.buildFrame(2, [0x7e, 0x7d]));
  eq(hex(f.slice(0, 8)), '7e 02 00 02 7d 5e 7d 5d');
  // only the leading byte may be a bare 0x7E
  if (f.slice(1).includes(0x7e)) throw new Error('bare 7E inside frame');
});
t('CRC bytes that equal 7E/7D are escaped and round-trip', () => {
  let hit7e = null, hit7d = null;
  for (let b = 0; b < 256 && !(hit7e && hit7d); b++) for (let c = 0; c < 256; c++) {
    const raw = P.rawFrame(2, [b, c]);
    const crcBytes = raw.slice(-2);
    if (!hit7e && crcBytes.includes(0x7e)) hit7e = [b, c];
    if (!hit7d && crcBytes.includes(0x7d)) hit7d = [b, c];
    if (hit7e && hit7d) break;
  }
  if (!hit7e || !hit7d) throw new Error('could not find payloads with CRC 7E/7D');
  [hit7e, hit7d].forEach((pl) => {
    const wire = P.buildFrame(2, pl);
    if (Array.from(wire).slice(1).includes(0x7e)) throw new Error('unescaped 7E on wire');
    const got = []; const p = new P.FrameParser((f) => got.push(f)); p.feed(wire);
    eq(got.length, 1); eq(got[0].crcOk, true); eq(got[0].payload, pl);
  });
});

// ---- SaveDrill payload, hand computed ----
const A = [[6, 5, 10, 26, 5], [14, 5, 10, 26, 5]].map(([horz, speed, spin, height, feed]) => ({ horz, speed, spin, height, feed }));
const B = [[3, 7, 10, 21, 5], [17, 7, 12, 21, 5]].map(([horz, speed, spin, height, feed]) => ({ horz, speed, spin, height, feed }));
const drill = { number: 5, name: 'AB', machinePos: 0, shuffle: false, balls: P.abBalls(A, B, 1) };
const EXPECT_PAYLOAD =
  '05 02 41 42 00 00 02 ' +                                   // drillNo=5, nameLen=2, "AB", machinePos=0, shuffle=0, nBalls=2
  '01 01 02  06 05 0a 1a 05  0e 05 0a 1a 05 ' +              // Random, loop 1, 2 shots: (6,5,10,26,5) (14,5,10,26,5)
  '01 01 02  03 07 0a 15 05  11 07 0c 15 05';                // Random, loop 1, 2 shots: (3,7,10,21,5) (17,7,12,21,5)
t('SaveDrill payload byte-for-byte', () => eq(hex(P.saveDrillPayload(drill)), EXPECT_PAYLOAD.replace(/\s+/g, ' ')));
t('SaveDrill shuffle flag and machine position', () => {
  const p = P.saveDrillPayload(Object.assign({}, drill, { shuffle: true, machinePos: 2 }));
  eq(p.slice(4, 7), [2, 1, 2]);
});
t('SaveDrill name must be ASCII', () => {
  let threw = false; try { P.saveDrillPayload(Object.assign({}, drill, { name: 'é' })); } catch (e) { threw = true; }
  if (!threw) throw new Error('expected throw');
});
t('6-shot A/B, 2 pairs: sizes', () => {
  const sh = [0, 1, 2].map((i) => ({ horz: 1 + i, speed: 5, spin: 10, height: 20, feed: 5 }));
  const pl = P.saveDrillPayload({ number: 24, name: 'AB RANDOM', machinePos: 0, shuffle: false, balls: P.abBalls(sh, sh, 2) });
  // 1+1+9+3 header + 4 balls x (3 + 3*5)
  eq(String(pl.length), String(14 + 4 * 18));
  eq(pl[pl.length - 1], 5);
});

// ---- parser round trip, chunked arrival ----
function parseAll(chunks) { const got = []; const p = new P.FrameParser((f) => got.push(f)); chunks.forEach((c) => p.feed(c)); return got; }
t('round trip in 20-byte chunks', () => {
  const wire = P.buildFrame(0x08, P.saveDrillPayload(drill));
  const got = parseAll(P.chunk(wire, 20));
  eq(String(got.length), '1'); eq(got[0].crcOk, true); eq(String(got[0].func), '8'); eq(hex(got[0].payload), EXPECT_PAYLOAD.replace(/\s+/g, ' '));
});
t('round trip one byte at a time, two frames back to back', () => {
  const w = Array.from(P.buildFrame(0x02, [24, 24])).concat(Array.from(P.buildFrame(0x17, [])));
  const got = parseAll(w.map((b) => [b]));
  eq(String(got.length), '2'); eq(String(got[0].func), '2'); eq(String(got[1].func), '23');
});
t('frame without leading 7E still parses (tolerance)', () => {
  const w = Array.from(P.buildFrame(0x17, [0])).slice(1);
  const got = parseAll([w]); eq(String(got.length), '1'); eq(got[0].crcOk, true);
});
t('corrupted CRC is flagged', () => {
  const w = Array.from(P.buildFrame(0x17, [0])); w[w.length - 1] ^= 0xff;
  const got = parseAll([w]); eq(String(got.length), '1'); eq(got[0].crcOk, false);
});

// ---- zero-length replies and generic un-escape (app: responseProcess) ----
t('bare 7E func 00 00 reply completes (no CRC)', () => {
  const got = parseAll([[0x7e, 0x01, 0x00, 0x00]]);
  eq(String(got.length), '1'); eq(String(got[0].func), '1'); eq(String(got[0].payload.length), '0'); eq(String(got[0].crcOk), 'true');
});
t('zero-length reply without leading 7E completes', () => { const got = parseAll([[0x20, 0, 0]]); eq(String(got.length), '1'); eq(String(got[0].func), '32'); });
t('zero-length reply split across chunks', () => { const got = parseAll([[0x7e, 0x17], [0x00], [0x00]]); eq(String(got.length), '1'); });
t('zero-length reply WITH trailing CRC: completes once, CRC swallowed, next bare frame intact', () => {
  const w = Array.from(P.buildFrame(0x17, [])).concat([0x18, 0x00, 0x00]); // second frame has no leading 7E
  const got = parseAll([w]);
  eq(String(got.length), '2'); eq(String(got[0].func), '23'); eq(String(got[1].func), '24');
});
t('requests with len 0 keep their CRC', () => {
  eq(hex(P.requests.getVersion()), '7e 18 00 00 ea c2'); // known vector
  const st = Array.from(P.requests.getStatus());
  eq(String(st.length), '6'); eq(hex(st.slice(0, 4)), '7e 17 00 00');
  eq(String((st[4] << 8) | st[5]), String(P.crc16([0x17, 0, 0])));
});
t('general un-escape: 7D xx -> xx ^ 0x20', () => {
  // payload byte 0x00 sent as 7D 20, plus a normal 7D 5E
  const raw = P.rawFrame(2, [0x00, 0x7e]);
  const wire = [0x7e]; raw.forEach((b) => { if (b === 0x00) wire.push(0x7d, 0x20); else if (b === 0x7e) wire.push(0x7d, 0x5e); else wire.push(b); });
  const got = parseAll([wire]); eq(String(got.length), '1'); eq(got[0].crcOk, true); eq(got[0].payload, [0x00, 0x7e]);
});

// ---- drill codes ----
const DEFAULT_CODE = 'A 10.9.11.21.6 12.10.12.17.6 14.8.8.20.6 B 10.9.13.15.11 7.7.9.20.11 13.7.11.21.11';
const sh5 = (a) => ({ horz: a[0], speed: a[1], spin: a[2], height: a[3], feed: a[4] });
t('parse default code', () => {
  const r = P.parseDrillCode(DEFAULT_CODE);
  eq(String(r.ok), 'true');
  eq(JSON.stringify(r.groups.A[0]), JSON.stringify(sh5([10, 9, 11, 21, 6])));
  eq(JSON.stringify(r.groups.B[2]), JSON.stringify(sh5([13, 7, 11, 21, 11])));
  eq(String(r.groups.A.length + r.groups.B.length), '6');
});
t('format -> parse round trip', () => {
  const g = { A: [[1, 1, 1, 1, 1], [19, 20, 19, 50, 20], [10, 5, 10, 20, 5]].map(sh5), B: [[3, 7, 10, 21, 5], [17, 7, 12, 21, 5], [10, 10, 10, 10, 10]].map(sh5) };
  const text = P.formatDrillCode(g);
  eq(text, 'A 1.1.1.1.1 19.20.19.50.20 10.5.10.20.5 B 3.7.10.21.5 17.7.12.21.5 10.10.10.10.10');
  const r = P.parseDrillCode(text);
  eq(JSON.stringify(r.groups), JSON.stringify(g));
  eq(P.formatDrillCode(P.parseDrillCode(DEFAULT_CODE).groups), DEFAULT_CODE);
});
t('parse tolerates case, newlines, extra whitespace', () => {
  const r = P.parseDrillCode('\n a 10.9.11.21.6\t12.10.12.17.6   14.8.8.20.6\nb 10.9.13.15.11 7.7.9.20.11\r\n13.7.11.21.11 \n');
  eq(String(r.ok), 'true'); eq(P.formatDrillCode(r.groups), DEFAULT_CODE);
});
function rejects(text, re) { const r = P.parseDrillCode(text); if (r.ok || r.groups) throw new Error('should reject: ' + text); if (!re.test(r.error)) throw new Error('error "' + r.error + '" does not match ' + re); }
t('code rejections (whole code, clear error)', () => {
  rejects('A 10.9.11.21.6 12.10.12.17.6 B 10.9.13.15.11 7.7.9.20.11 13.7.11.21.11', /group A has 2 shots/);
  rejects('A 10.9.11.21.6 12.10.12.17.6 14.8.8.20.6 B 10.9.13.15.11 7.7.9.20.11', /group B has 2 shots/);
  rejects('A 10.9.11.21.6 12.21.12.17.6 14.8.8.20.6 B 10.9.13.15.11 7.7.9.20.11 13.7.11.21.11', /shot A2 speed 21 is out of range 1-20/);
  rejects('A 10.9.11.21.6 12.10.12.17.6 14.8.8.20.6 B 10.9.13.15.11 7.7.9.20.11 13.7.11.51.11', /shot B3 height 51 is out of range 1-50/);
  rejects('A 10.9.11.21.6 12.10.12.17.6 14.8.8.20.6 B 10.9.13.15.11 7.7.9.20.11 13.7.11.21.0', /shot B3 feed 0 is out of range 1-20/);
  rejects('A 20.9.11.21.6 12.10.12.17.6 14.8.8.20.6 B 10.9.13.15.11 7.7.9.20.11 13.7.11.21.11', /shot A1 horz 20/);
  rejects('A 10.9.11.21.6 12.10.12.17.6 14.8.8.20.6 B 10.9.20.15.11 7.7.9.20.11 13.7.11.21.11', /shot B1 spin 20/);
  rejects('A 10.9.5.21.6 12.10.12.17.6 14.8.8.20.6 B 10.9.13.15.11 7.7.9.20.11 13.7.11.21.11'.replace('10.9.5', '10.9.5.5'), /shot A1 .* must be 5 whole numbers/);
  rejects('A 10.9.11.21.6 12.10.12.17.6 14.8.8.20.6 B 10.9.13.15.11 7.7.9.20.11 13.7.11.21.11'.replace('10.9.11.21.6', '10.9.11.21.6.5'), /5 whole numbers/);
  rejects('A 10.9.11.21.6 12.10.12.17.6 14.8.8.20.6 B 10.9.13.15.11 7.7.9.20.11 13.7.11.21.11'.replace('12.10', '12.x'), /shot A2/);
  rejects('A 10.9.11.21.6 12.10.12.17.6 14.8.8.20.6 B 10.9.13.15.11 7.7.9.20.11 13.7.11.-1.11', /shot B3/);
  rejects('A 10.9.11.21.6 12.10.12.17.6 14.8.8.20.6 B 10.9.13.15.11 7.7.9.20.11 13.7.11.21.11 13.7.11.21.11', /more than 3 shots/);
  rejects('A 10.9.11.21.6 12.10.12.17.6 14.8.8.20.6', /group B is missing/);
  rejects('B 10.9.13.15.11 7.7.9.20.11 13.7.11.21.11', /group A is missing/);
  rejects('10.9.11.21.6 A 10.9.11.21.6', /must start with A or B/);
  rejects('A 1.1.1.1.1 1.1.1.1.1 1.1.1.1.1 A 1.1.1.1.1', /appears twice/);
  rejects('', /empty/);
  rejects('hello', /must start with A or B/);
});
t('B-before-A order is accepted', () => {
  const r = P.parseDrillCode('B 10.9.13.15.11 7.7.9.20.11 13.7.11.21.11 A 10.9.11.21.6 12.10.12.17.6 14.8.8.20.6');
  eq(String(r.ok), 'true'); eq(P.formatDrillCode(r.groups), DEFAULT_CODE);
});

// ---- other requests and parsers ----
t('VerifyCode frames', () => {
  const raw = (kind, code) => P.rawFrame(0x20, [kind, code ? 1 : 0].concat(code ? Array.from(code, (c) => c.charCodeAt(0)) : [0, 0, 0, 0, 0, 0], [0]));
  eq(hex(P.requests.verify(1, '123456')), hex([0x7e].concat(raw(1, '123456'))));
  eq(hex(P.requests.verify(0, '')), hex([0x7e].concat(raw(0, ''))));
  eq(hex(P.rawFrame(0x20, [1, 1, 0x31, 0x32, 0x33, 0x34, 0x35, 0x36, 0]).slice(0, 12)), '20 00 09 01 01 31 32 33 34 35 36 00');
});
t('SetCurrentDrill repeat', () => {
  eq(hex(P.repeatSlots(24, 1)), '18'); eq(String(P.repeatSlots(24, 10).length), '10');
  eq(hex(Array.from(P.requests.setCurrent([24, 24])).slice(0, 6)), '7e 02 00 02 18 18');
});
t('parseVersion / needsPin', () => {
  const s = 'TITAN7.4.1P\0\0\0';
  const raw = [0x18, 0, 14].concat(Array.from(s, (c) => c.charCodeAt(0)));
  const v = P.parseVersion(raw);
  eq(String(v.major) + '.' + v.minor + '.' + v.patch + v.mode, '7.4.1P'); eq(String(v.needsPin), 'true');
  const ver = (str) => P.parseVersion([0x18, 0, 14].concat(Array.from(str, (c) => c.charCodeAt(0))));
  eq(String(ver('TITAN7.3.1P\0\0\0').needsPin), 'false', 'minor 3 -> no auto PIN');
  eq(String(ver('TITAN7.4.1Q\0\0\0').needsPin), 'true', 'Q is pickle');
  eq(String(ver('TITAN7.4.1D\0\0\0').needsPin), 'false', 'D is tennis, mode mismatch -> no auto PIN');
  eq(ver('TITAN7.4.1D\0\0\0').modeName, 'tennis');
});
t('parseVerify / parseStatus', () => {
  eq(P.parseVerify({ payload: [0] }), 'ok'); eq(P.parseVerify({ payload: [1] }), 'failed'); eq(P.parseVerify({ payload: [2] }), 'needPin');
  eq(P.parseStatus({ payload: [2, 5] }).name, 'STOP'); eq(P.parseStatus({ payload: [7] }).name, 'ALARM');
});
t('labels', () => {
  eq(P.label('horz', 3), '10° left'); eq(P.label('horz', 10), 'straight'); eq(P.label('horz', 19), '11° right');
  eq(P.label('speed', 7), '29 mph'); eq(P.label('speed', 1), '5 mph'); eq(P.label('speed', 20), '70 mph');
  eq(P.label('spin', 10), 'no spin'); eq(P.label('spin', 1), '2700 rpm backspin'); eq(P.label('spin', 19), '2700 rpm topspin');
  eq(P.label('height', 1), '6°'); eq(P.label('height', 21), '31°'); eq(P.label('height', 50), '60°');
  eq(P.label('feed', 5), '3 s'); eq(P.label('feed', 18), '9.5 s'); eq(P.label('feed', 20), '20 s');
});
t('clampShot', () => eq(JSON.stringify(P.clampShot({ horz: 99, speed: 0, spin: 10.4, height: 'x', feed: 5 })), '{"horz":19,"speed":1,"spin":10,"height":1,"feed":5}'));

// ---- cross-check against titan_frames.py (run isolated with -I, script path as an argument) ----
if (SCRIPT && require('fs').existsSync(SCRIPT)) t('frames match titan_frames.py (GetVersion, START, ExecManual, SetCurrentDrill, SaveDrill)', () => {
  const out = execFileSync('python3', ['-I', SCRIPT], { encoding: 'utf8' });
  const py = {};
  out.split('\n').forEach((ln) => { const m = ln.match(/^(\S+)\s+((?:[0-9a-f]{2} )+[0-9a-f]{2})\s+\(/); if (m) py[m[1]] = m[2]; });
  eq(py.get_version, hex(P.requests.getVersion()), 'GetVersion');
  eq(py.control_start, hex(P.requests.control('START')), 'START');
  eq(py.control_stop, hex(P.requests.control('STOP')), 'STOP');
  eq(py.exec_manual_speed_7, hex(P.requests.execManual(1, 7)), 'ExecManual');
  eq(py['set_current_drill_[5]'], hex(P.requests.setCurrent([5])), 'SetCurrentDrill');
  // python example: drill 5 "AB", random A = [(6,5,10,26,5),(14,5,10,26,5)], random B = [(3,7,10,21,5),(17,7,12,21,5)]
  eq(py.save_drill_5_A_then_B, hex(P.requests.saveDrill(drill)), 'SaveDrill');
});

console.log('\n' + passed + ' passed, ' + failed + ' failed');
process.exit(failed ? 1 : 0);
