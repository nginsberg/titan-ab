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
- Group A / Group B: three shots each, five sliders per shot: horizontal, speed, spin, height, feed. Each shows the raw level plus its unit (degrees left/right, mph, rpm, degrees, seconds), taken from the app's own tables. Edits are saved in the browser. On load, a saved shot with any field missing, not a number or out of range resets to its default shot (nothing is clamped). Defaults come from the app's pickleball preset table (machine position 0): A is the speed-9 row, B is the speed-5 row, all with no spin.
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
