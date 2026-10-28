# Titan A/B drill (prototype)

One static page that drives a Titan ACE pickleball machine over Bluetooth: it alternates one random shot from group A (3 shots) with one random shot from group B (3 shots), on repeat. No framework, no build step. The protocol comes from static analysis of the Android app and has **not been verified on hardware**.

Files: `index.html` (UI and transport), `protocol.js` (frame and drill encoding, also usable from node), `test.js` (`node test.js`).

## Open it in Bluefy
1. Serve the folder over https or localhost (Web Bluetooth needs a secure context). From a laptop on the same Wi-Fi, for example: `cd titan-ab && python3 -m http.server 8000`, then open `http://<laptop-ip>:8000/` in Bluefy. A plain http LAN address may be refused as insecure; if so, use an https tunnel or host the two files anywhere with https.
2. Tap Connect and pick the machine. If it is not listed, tick "Scan all devices" and try again.
3. If the machine shows a 6-digit code, type it into Machine PIN and tap Send PIN. The PIN is remembered on the phone.

## Controls
- Connect / Reconnect: Web Bluetooth connect, starts notifications, asks the machine for its version, then runs the PIN check if the version needs it.
- Dry run: affects Start only. Nothing is sent; every frame is only written to the log. Use it on a laptop to check the page. It is off by default when the browser has Web Bluetooth, is not remembered, and is switched off and locked while connected. STOP and Pause always go to the wire when connected.
- Group A / Group B: three shots each, five sliders per shot: horizontal, speed, spin, height, feed. Each shows the raw level plus its unit (degrees left/right, mph, rpm, degrees, seconds), taken from the app's own tables. Edits are saved in the browser. On load, a saved shot with any field missing, not a number or out of range resets to its default shot (nothing is clamped). Defaults (also what "Reset shots to defaults" restores): `A 10.9.11.21.6 12.10.12.17.6 14.8.8.20.6 B 10.9.13.15.11 7.7.9.20.11 13.7.11.21.11`.
- Drill code: paste a code of that form into the Drill code box and tap Load code to set all 6 shots at once, or tap Copy code to copy the current 6 shots. Each shot is `horz.speed.spin.height.feed` as 1-based levels; A and B can be upper or lower case and whitespace or newlines are fine. The code must have exactly 3 shots in each group, whole numbers only, each within its range (horz 1-19, speed 1-20, spin 1-19, height 1-50, feed 1-20). Any bad value rejects the whole code with a message naming the shot and field, and nothing changes (no clamping). Loading is blocked while a drill is starting.
- On-court test settings (collapsed): drill slot (default 24), pairs per drill (default 2, max 8), repeat count (default 10, sent as the slot number repeated N times in Set Current Drill, i.e. a Mega Drill), machine position (0-3, default 0; anything else becomes 0). The drill slot is overwritten on the machine each time you press Start, so the default slot 24 replaces whatever drill is saved in slot 24. One pair is one Random ball from A then one from B, loop 1. More than 2 pairs is more than the official app's 5 random balls per drill and may be rejected.
- Start drill: confirm dialog, then Save Drill, Set Current Drill, Control START, each result shown. It stops at the first missing or invalid reply.
- STOP (red, fixed at the bottom): drops everything queued, cancels anything waiting (including a running Start sequence), and sends Control STOP right away. If a Bluetooth write looks stuck (over 0.3 s), STOP is written directly instead of waiting behind it, and it retries once if there is no reply. A red banner appears if no STOP write has completed within 1.5 s, and its wording says whether STOP was written at all. Each Bluetooth write times out after 1 s. Frames still waiting behind a slow write, and the unsent chunks of a half-written frame, are dropped when STOP or Pause is pressed, so nothing queued can reach the machine after STOP. Connect/Reconnect is disabled while the machine reports START, PENDING_START, MOTO_STARTING or TESTING. Pause cancels a running Start sequence the same way, then sends Control PAUSE.
- If Start's last step (Control START) was written but its reply never arrives, the page says "START was sent; the machine may be running. Press STOP."
- Status chip: polls Get Robot Status about once a second, except while a Start sequence is running. A disconnect shows a red banner.
- Log: every TX and RX in hex with timestamps. "Copy log" copies it so you can send it back.

## On-court test checklist
1. Dry run on a laptop first: Start, open the log, check three TX lines (SaveDrill, SetCurrentDrill, Control START).
2. On the phone in Bluefy: Connect. Confirm a version appears and the status chip shows a status (probably STOP). If there is a PIN, enter it and confirm "PIN ok".
3. Machine loaded with a few balls, area clear. Set pairs = 1, repeat = 1 for the first run. Tap Start. Expect: three step results, status goes to START, balls fire.
4. Check that A and B alternate and each group's three shots vary at random. Compare speed, height and direction with the labels.
5. Tap Pause, then Start again, then STOP. Confirm STOP halts the machine within a second and the status chip shows STOP.
6. Raise to pairs = 2, repeat = 10 (the defaults). Then try pairs = 3 and note whether the machine rejects it.
7. Walk away from the phone to force a disconnect; confirm the red banner appears and the machine's own controls still stop it.
8. Open the Log, tap Copy log, and send it back, including any step that failed.

## What is confirmed vs assumed
Confirmed in the decompiled app (protocol/*.dart, common/level_mapping_utils.dart): frame layout, escaping and CRC; function codes; SaveDrill byte order; Ball and Shot records; 20-byte write chunks; slider level to unit tables; VerifyCode payload shape and reply codes; status codes and the version-string positions.
Not confirmed:
- Whether the machine's replies carry the leading 0x7E (the parser accepts both) and whether reply CRCs follow the same rule. A reply that fails its CRC aborts the start sequence. Reply parsing follows the app's `responseProcess`: a reply with length 0 completes after the 3 header bytes with no CRC check (a trailing CRC is swallowed if present), and `7D xx` un-escapes to `xx ^ 0x20`. Requests the page sends always carry their CRC.
- What an "ok" reply looks like for SaveDrill, SetCurrentDrill and Control. The page only requires a CRC-valid reply with the same function code and shows the payload.
- The meaning of `loop` on a Random ball (assumed 1 = used once). The app's own preset drills contain no Random balls.
- Whether the same slot number repeated in Set Current Drill is accepted, the firmware limits (random balls per drill, balls per drill, Mega Drill length), and which machine positions allow Random balls (the app shows "Current position of machine does not support random shot" in some cases; the condition was not located).
- The PIN rule (resolved from `response/get_version_response.dart` and `pages/linking`): the mode letter in the version string maps P or Q to pickleball and D (or anything else) to tennis. The app sends the automatic VerifyCode only when that mode equals the app's own mode and the version reads major >= 7 and minor >= 4 (taken literally). This page behaves as the pickleball app, so the automatic check runs only for P or Q. The PIN box can always be used by hand. VerifyCode `kind` is 0 for the automatic check and 1 for a code typed by the user.
- Whether the Titan advertises the Nordic UART service so the default device filter finds it (hence "Scan all devices").
- Writes use "write with response" when the browser offers it. Bluefy's support for that was not checked.

## Laptop workflow (Python daemon + CLI)
The `titan/` package drives the machine from a MacBook. One process, the daemon, holds the Bluetooth link. Every other command is a thin client that talks to it over a unix socket (`runs/titan.sock`) and never touches Bluetooth. This split exists because only a Terminal.app process can get macOS Bluetooth permission; processes started by the assistant's shell crash on any CoreBluetooth use.

### Owner: start the daemon
Double-click `start-daemon.command` in Finder, or run `open -a Terminal start-daemon.command`. The first time, macOS asks to allow Terminal to use Bluetooth: allow it. Close the Titan phone app first so the machine is free.
- With no saved address it scans for a device advertising the Nordic UART service (or with "titan" in its name). If it finds exactly one it connects and saves the address in `runs/device.json`; if it finds several it lists them (name, address, signal) and connects to none: restart with `./start-daemon.command --address <uuid>`. Once connected it only ever reconnects to that same machine. If the saved machine is not found it keeps retrying and does not scan for others; to switch machines use `--address` or delete `runs/device.json`.
- On connect, if the machine already reports a running state, the window shows a loud MACHINE IS ALREADY RUNNING line.
- If the machine asks for a PIN and none is saved or given, the window shows a loud PIN NEEDED line and the status line keeps saying PIN NEEDED. It never prompts, so the STOP key stays live. The assistant then runs `.venv/bin/python -m titan pin NNNNNN` with the digits on the machine's screen, or the owner restarts with `--pin NNNNNN`. An accepted PIN is saved in `runs/pin`.
- In the daemon window Ctrl-Z and Ctrl-S are disabled so they cannot suspend or freeze the STOP key.
- **That window is the emergency STOP.** SPACE, ENTER or `s` = STOP. `p` = PAUSE. `q` = STOP and quit. Ctrl-C, closing the window, or a crash also send STOP before disconnecting. Keep the window in front while the machine has balls in it.
- If no STOP write gets through, the window shows a huge `STOP NOT SENT - USE MACHINE POWER BUTTON`. If the link drops it says so loudly and keeps reconnecting; the machine may still be running, so use its power button.
- `--fake` runs a simulated machine with no Bluetooth (for trying the commands).

### Assistant: commands
Run from the project folder as `.venv/bin/python -m titan <command>`. Output is short plain text; a nonzero exit means it failed or was refused.
- `status`: daemon up, connected, version, machine status, last command, calibration in effect. `log [-n 40] [--all]`: tail of `runs/session-YYYYMMDD.log` (every TX/RX frame in hex and every command; status polls hidden unless `--all`).
- `stop`, `pause`, `start`. Note: `start` resumes whatever drill is current on the machine, with no speed check; it does not know what that drill contains. STOP skips the queue and cancels anything in progress, including a half-sent frame, so a START that has not gone out yet never goes out after it.
- `fire h.s.sp.ht.f [--via drill|balltest] [--raw] [--allow-fast]`: one ball. `--via drill` (default) saves a one-ball drill (one Regular ball, loop 1) to a slot chosen like `drill` does (see Slot below), sets it current and sends START. `--via balltest` sends BallTest (0x04) with the same ball, then START. Each step's reply is printed. Which path fires exactly one ball is still an on-court experiment.
- `drill ["A h.s.sp.ht.f x3 B h.s.sp.ht.f x3"] [--pairs 2] [--repeat 1] [--slot N] [--half right|left] [--out-ball CODE --out-every 12] [--raw] [--allow-fast]`: Random ball from A, Random ball from B, `pairs` times, saved to a slot, then Set Current Drill with the slot repeated `repeat` times, then START. Codes are parsed strictly and never clamped. With no code it uses the one that worked on court: `A 13.9.11.22.3 15.10.12.18.3 19.8.8.21.3 B 15.9.13.15.9 12.7.9.20.9 19.7.11.21.9` (right half; A is a deep return, 2 s before B; B is midcourt at the feet, 5 s before the next A).
  - Feed is the pause after that ball, not before it.
  - Slot: the machine only accepts SaveDrill for an existing slot or the next free one (it stores drills contiguously 1..N); anything else gets no reply and swallows the next frame. So `drill` first sends GetCrcList (2 bytes per stored drill) to count N, reuses the slot it saved to last time (remembered in `runs/drill_slot.json`) if that is <= N, otherwise saves to N+1, and refuses if the machine already holds 24. `--slot` overrides but must be <= N+1. A missing SaveDrill reply fails the sequence.
  - Repeat: default 1. The saved drill loops natively on the machine after Set Current Drill + START, with no motor re-initialization between balls.
  - `--half left` mirrors every horz as 20 - h (codes are written for the right half; horz above 10 lands to the player's right as they face the machine). Default right.
  - `--out-ball CODE` (a shot, written for the right half) makes the last pair's B ball a Random ball of [B1, B3, OUT], so about 1 B ball in `--out-every` (default 12) is out: pairs = round(N/3), so 12 gives 4 pairs and 1 in 12; an N that is not a multiple of 3 is approximate (the real rate is 1 in 3 x pairs). It conflicts with `--pairs`. Today's out ball: `15.10.10.20.9`. It goes through the same calibration, mirror and speed-cap checks as every other shot.
- `stream [code] [--pairs 20] [--half right|left]`: no SaveDrill. The laptop picks a random A, then a random B, fires each as a BallTest plus START and waits that shot's feed time. Slower than a saved drill (BallTest re-initializes before every ball). STOP ends it.
- `raw FUNC [HEXPAYLOAD] [--timeout S] [--unsafe]`: debug, one frame and its reply. FUNC must be 0-255 and the timeout at most 10 s. Control START and the BallTest, ExecManual, SaveDrill and SetCurrentDrill frames are refused unless `--unsafe`. It is also refused while a STOP is in progress, a sequence is running or a stream is running.
- While a `stream` is running, `fire`, `drill`, `start` and `raw` are refused with "stream running, STOP it first".
- `calib show | set [--horz-deg X] [--height-deg Y] | reset`: aim offsets in degrees (horz positive = further right, height positive = higher), stored in `runs/calibration.json`, applied to every `fire` and `drill` unless `--raw`. Each level is converted to degrees with the app's tables, the offset is added, and the nearest level is used; the before and after levels are printed. If the adjusted angle is outside what the machine can do, the command is refused.
- Safety cap: any shot above speed level 14 (50 mph) is refused unless `--allow-fast`. The daemon checks this too.
- `fire --via drill` and `drill` both pick their slot from GetCrcList (see the slot rule above) and overwrite only the slot they saved last time.
- A calibration file with a bad or non-finite value makes `fire` and `drill` refuse (unless `--raw`) until it is fixed or `calib reset`.
- If a command gets no answer from the daemon, the sequence may still be running: check `status` or run `stop`.
- `pin 123456`: send the machine PIN.

Calibration loop on court: `fire` a shot, ask the owner where it landed, `calib set --horz-deg ... --height-deg ...`, fire again.

Tests: `.venv/bin/python -m pytest -q -p no:cacheprovider tests/` (protocol vectors, a byte-for-byte cross-check against `protocol.js` via node, drill codes, calibration, and the daemon end to end with `--fake` over the socket, including STOP while a slow write is pending and STOP with every write hung).

The machine also pushes frames nobody asked for: func 0x11 `[drill, ballIndex]` (the ball it is on) and func 0x12 (voltage). The log labels them "machine push".
