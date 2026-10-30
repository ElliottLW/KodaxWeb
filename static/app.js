"use strict";

const $ = (sel) => document.querySelector(sel);
const $$ = (sel) => [...document.querySelectorAll(sel)];

const state = { cameras: [], current: null, status: null, live: null, zones: null, audio: false };

// ── helpers ────────────────────────────────────────────────────────────────

async function api(path, options = {}) {
  const init = { ...options, headers: { "Content-Type": "application/json" } };
  if (init.body && typeof init.body !== "string") init.body = JSON.stringify(init.body);
  const resp = await fetch(path, init);
  const data = await resp.json().catch(() => ({}));
  if (!resp.ok) throw new Error(data.error || `HTTP ${resp.status}`);
  return data;
}

let toastTimer;
function toast(message, isError = false) {
  const el = $("#toast");
  el.textContent = message;
  el.classList.toggle("error", isError);
  el.classList.add("show");
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => el.classList.remove("show"), isError ? 5000 : 2200);
}

function show(view) {
  for (const id of ["view-empty", "view-camera", "view-setup"]) $("#" + id).hidden = id !== view;
}

const esc = (s) => String(s ?? "").replace(/[&<>"]/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c]));

// ── camera list ────────────────────────────────────────────────────────────

async function loadCameras(selectId) {
  state.cameras = await api("/api/cameras");
  const list = $("#camera-list");
  list.innerHTML = state.cameras
    .map((c) => `<button data-id="${c.id}"><span>${esc(c.name)}</span><small>${esc(c.ip)}</small></button>`)
    .join("");
  list.querySelectorAll("button").forEach((b) => (b.onclick = () => selectCamera(b.dataset.id)));
  const target = selectId || state.current?.id || state.cameras[0]?.id;
  if (target) selectCamera(target);
  else show("view-empty");
}

async function selectCamera(id) {
  stopLive();
  closeZones(false);
  state.current = state.cameras.find((c) => c.id === id);
  if (!state.current) return;
  $$("#camera-list button").forEach((b) => b.setAttribute("aria-current", b.dataset.id === id));
  $("#cam-name").textContent = state.current.name;
  $("#cam-sub").textContent = `${state.current.ip} · ${state.current.mac}`;
  $("#setup-log").hidden = true;
  show("view-camera");
  await refreshStatus();
}

async function refreshStatus() {
  const cam = state.current;
  $("#tiles").innerHTML = `<div class="tile wide"><div class="label">Status</div><div class="sub">Loading…</div></div>`;
  try {
    const s = await api(`/api/cameras/${cam.id}/status`);
    if (state.current !== cam) return;
    state.status = s;
    renderStatus(s);
  } catch (err) {
    $("#tiles").innerHTML = `<div class="tile wide"><div class="label">Offline</div><div class="sub">${esc(err.message)}</div></div>`;
  }
}

function renderStatus(s) {
  const bat = s.battery;
  const sdOk = s.sd_card.capacity_mb > 0;
  $("#tiles").innerHTML = `
    <div class="tile"><div class="label">Battery</div><div class="value">${bat}%</div>
      <div class="sub">${s.charging ? "Charging" : "On battery"}</div>
      <div class="bar ${bat < 20 ? "low" : ""}"><i style="width:${Math.max(0, bat)}%"></i></div></div>
    <div class="tile"><div class="label">Wi-Fi</div><div class="value">${s.wifi_strength}%</div>
      <div class="sub">${esc(s.ssid)}</div>
      <div class="bar ${s.wifi_strength < 30 ? "low" : ""}"><i style="width:${s.wifi_strength}%"></i></div></div>
    <div class="tile"><div class="label">Firmware</div><div class="value" style="font-size:18px">${esc(s.firmware)}</div>
      <div class="sub">media ${esc(s.versions.media || "?")}</div></div>
    <div class="tile"><div class="label">SD card</div><div class="value" style="font-size:18px">${sdOk ? `${(s.sd_card.free_mb / 1024).toFixed(1)} GB free` : "None"}</div>
      <div class="sub">${sdOk ? `of ${(s.sd_card.capacity_mb / 1024).toFixed(1)} GB` : "No card detected"}</div></div>
    ${s.temperature !== null ? `<div class="tile wide"><div class="label">Room</div><div class="value">${s.temperature.toFixed(1)}°C · ${s.humidity ?? "?"}%</div></div>` : ""}`;

  for (const el of $$("[data-key]")) {
    const value = s.settings[el.dataset.key];
    if (value === undefined) continue;
    if (el.type === "checkbox") {
      el.checked = !!value;
    } else {
      if (el.tagName === "SELECT" && ![...el.options].some((o) => o.value === String(value))) {
        el.add(new Option(String(value), String(value)));
      }
      el.value = value;
    }
  }
  showNightMode(s.settings.night_vision);
  $("#btn-ptz").hidden = !s.pan_tilt;
  $("#ptz-misc").hidden = !s.pan_tilt;
  $("#ptz-uncalibrated").hidden = !s.pan_tilt || s.pan_tilt_calibrated;
  if (!s.pan_tilt) showPtz(false);
  $("#sd-info").textContent = sdOk ? "" : "No SD card detected; these settings only apply with a card inserted.";
  $("#wifi-info").innerHTML = `
    <dt>Network</dt><dd>${esc(s.ssid)}</dd><dt>Signal</dt><dd>${s.wifi_strength}%</dd>
    <dt>IP address</dt><dd>${esc(s.local_ip)}</dd><dt>DNS</dt><dd>${esc(s.dns.join(", "))}</dd>`;
  $("#device-info").innerHTML = `
    <dt>MAC</dt><dd>${esc(s.mac)}</dd><dt>UDID</dt><dd>${esc(s.udid)}</dd>
    <dt>Firmware</dt><dd>${esc(Object.entries(s.versions).map(([k, v]) => `${k} ${v}`).join(", "))}</dd>
    <dt>Hardware</dt><dd>hw ${esc(s.hardware_id)} · SoC ${esc(s.soc_version)}</dd>
    <dt>Charging time</dt><dd>${s.charge_minutes} min</dd>`;
}

// ── settings ───────────────────────────────────────────────────────────────

async function saveSetting(key, value) {
  try {
    await api(`/api/cameras/${state.current.id}/settings`, { method: "POST", body: { [key]: value } });
    toast("Saved");
  } catch (err) {
    toast(`Not saved: ${err.message}`, true);
  }
  refreshStatus();
}

function bindSettings() {
  for (const el of $$("[data-key]")) {
    el.addEventListener("change", () =>
      saveSetting(el.dataset.key, el.type === "checkbox" ? el.checked : Number(el.value)));
  }
}

// ── night vision (toolbar button cycles Auto → On → Off) ───────────────────

const NIGHT_MODES = { 0: "Auto", 1: "On", 2: "Off" };

function showNightMode(mode) {
  const btn = $("#btn-night");
  btn.dataset.mode = mode;
  $("#night-label").textContent = NIGHT_MODES[mode] ?? "Auto";
  btn.setAttribute("aria-label", `Night vision: ${NIGHT_MODES[mode] ?? "Auto"}. Click to change`);
}

function cycleNightMode() {
  const next = (Number($("#btn-night").dataset.mode || 0) + 1) % 3;
  showNightMode(next); // instant feedback; refreshStatus confirms
  saveSetting("night_vision", next);
}

// ── live view (WebCodecs) ──────────────────────────────────────────────────

function codecFromFrame(bytes) {
  for (let i = 0; i + 8 < bytes.length; i++) {
    if (bytes[i] === 0 && bytes[i + 1] === 0 && bytes[i + 2] === 1 && (bytes[i + 3] & 0x1f) === 7) {
      const hex = (n) => n.toString(16).padStart(2, "0");
      return `avc1.${hex(bytes[i + 4])}${hex(bytes[i + 5])}${hex(bytes[i + 6])}`;
    }
  }
  return null;
}

function startLive() {
  if (!("VideoDecoder" in window)) {
    $("#live-msg").textContent = window.isSecureContext
      ? "This browser can't decode video (WebCodecs missing). Use a current Chrome, Edge, Firefox or Safari."
      : "Live view needs a secure page: open it via http://localhost or over HTTPS.";
    return;
  }
  const cam = state.current;
  const canvas = $("#live");
  const ctx = canvas.getContext("2d");
  const msg = $("#live-msg");
  msg.hidden = false;
  msg.textContent = "Connecting to camera…";
  $("#btn-play").hidden = true;
  $("#live-toolbar").hidden = false;
  $("#btn-snap").disabled = true;
  wakeToolbar();
  let decoder = null;
  let configured = null;
  const shown = []; // timestamps of recently drawn frames, for the fps readout
  playout.reset();

  const newDecoder = (codec) => {
    const d = new VideoDecoder({
      output: (frame) => {
        // Draw straight away and release: the decoder has only a few output
        // buffers, so holding frames would stall it.
        if (canvas.width !== frame.displayWidth) {
          canvas.width = frame.displayWidth;
          canvas.height = frame.displayHeight;
        }
        ctx.drawImage(frame, 0, 0);
        frame.close();
        msg.hidden = true;
        $("#btn-snap").disabled = false;
        const now = performance.now();
        shown.push(now);
        while (shown.length && now - shown[0] > 3000) shown.shift();
        $("#live-stats").textContent = `${(shown.length / 3).toFixed(1)} fps`;
      },
      // A corrupt or lost frame: rebuild at the next keyframe, don't stop.
      error: () => { if (decoder === d) decoder = null; },
    });
    d.configure({ codec, optimizeForLatency: true });
    configured = codec;
    return d;
  };

  // Encoded frames wait here until their capture time comes round on the
  // playout clock, so bursts from the camera's Wi-Fi play back evenly.
  const DECODE_LEAD_MS = 15; // decoding takes a few ms
  const pending = [];
  const feed = (data, captureMs) => {
    const codec = codecFromFrame(data); // keyframes carry SPS; P-frames don't
    if (codec && (!decoder || decoder.state === "closed" || codec !== configured)) {
      try { decoder?.close(); } catch {}
      decoder = newDecoder(codec);
    }
    if (!decoder || decoder.state !== "configured") return; // wait for a keyframe
    try {
      decoder.decode(new EncodedVideoChunk({ type: codec ? "key" : "delta", timestamp: captureMs * 1000, data }));
    } catch {
      decoder = null;
    }
  };
  // Timer-driven (not requestAnimationFrame, which browsers slow to ~1/s for
  // background tabs and unfocused panes and would release frames in clumps).
  let pumpTimer = null;
  const pump = () => {
    pumpTimer = null;
    if (!state.live || state.live.ws !== ws) return;
    const now = performance.now();
    while (pending.length && playout.dueAt(pending[0][1]) - DECODE_LEAD_MS <= now) {
      const [data, captureMs] = pending.shift();
      feed(data, captureMs);
    }
    const wait = pending.length ? playout.dueAt(pending[0][1]) - DECODE_LEAD_MS - now : 20;
    pumpTimer = setTimeout(pump, Math.max(1, Math.min(wait, 100)));
  };

  const ws = new WebSocket(`${location.protocol === "https:" ? "wss" : "ws"}://${location.host}/api/cameras/${cam.id}/live`);
  ws.binaryType = "arraybuffer";
  ws.onmessage = (event) => {
    if (typeof event.data === "string") {
      const info = JSON.parse(event.data);
      const btn = $("#btn-audio");
      btn.disabled = !info.audio_available;
      btn.title = info.audio_available ? "Sound" : "Sound needs PyAV on the server (py -m pip install av)";
      if (state.audio) ws.send(JSON.stringify({ audio: true })); // keep sound on across reconnects
      return;
    }
    const message = new Uint8Array(event.data);
    const captureMs = new DataView(event.data).getUint32(1);
    playout.arrived(captureMs);
    if (message[0] === 2) return playAudio(message.subarray(5), captureMs);
    pending.push([message.subarray(5), captureMs]);
    if (pending.length > 60) pending.shift(); // never let it build up
    if (pumpTimer === null) pump();
  };
  pump();
  ws.onclose = () => {
    if (state.live?.ws === ws) {
      stopLive(shown.length ? "Stream ended" : "Couldn't start the stream (camera busy or offline?)");
    }
  };
  state.live = { ws, close: () => decoder?.close() };
}

// ── playout clock ──────────────────────────────────────────────────────────
// The camera's Wi-Fi goes quiet for ~0.5 s every second or two, then delivers
// the held-back frames in a burst. Each frame carries its capture time, so we
// show it at capture time + the quickest delivery seen + a jitter allowance.

const playout = {
  minTransit: Infinity, // ms: fastest capture→arrival seen (network best case)
  allowance: 700, // ms of delay to absorb: the worst recent stall + a margin
  recent: [], // [arrival time, delay beyond best case] over the last minute
  reset() { this.minTransit = Infinity; this.allowance = 700; this.recent = []; },
  arrived(captureMs) {
    const now = performance.now();
    const transit = now - captureMs;
    // Track the best case, slowly forgetting it so clock drift can't build up.
    this.minTransit = Math.min(transit, this.minTransit + 0.032);
    this.recent.push([now, transit - this.minTransit]);
    while (this.recent[0][0] < now - 60000) this.recent.shift();
    // Size the buffer to the worst stall seen in the last minute. Grow at
    // once; shrink by 2 ms a frame so playback just runs ~3% fast for a while
    // instead of showing a burst of suddenly-overdue frames.
    const worst = Math.max(...this.recent.map((r) => r[1]));
    const target = Math.min(1500, Math.max(300, worst + 100));
    this.allowance = target > this.allowance ? target : Math.max(target, this.allowance - 2);
  },
  dueAt(captureMs) { return captureMs + this.minTransit + this.allowance; },
};

// ── live audio (16 kHz PCM from the server's G.722 decoder) ────────────────

const AUDIO_RATE = 16000;
let audioCtx = null;
let audioNext = 0;

function playAudio(bytes, captureMs) {
  if (!state.audio || !audioCtx) return;
  // Copy: after the 5-byte prefix the samples aren't 2-byte aligned.
  const pcm = new Int16Array(bytes.slice().buffer, 0, bytes.byteLength >> 1);
  const buffer = audioCtx.createBuffer(1, pcm.length, AUDIO_RATE);
  const out = buffer.getChannelData(0);
  for (let i = 0; i < pcm.length; i++) out[i] = pcm[i] / 32768;
  const src = audioCtx.createBufferSource();
  src.buffer = buffer;
  src.connect(audioCtx.destination);
  // Same clock as the video, so picture and sound stay in sync.
  let start = audioCtx.currentTime + (playout.dueAt(captureMs) - performance.now()) / 1000;
  if (Math.abs(start - audioNext) < 0.08) start = audioNext; // join chunks seamlessly
  if (start < audioCtx.currentTime) return; // too late to play
  src.start(start);
  audioNext = start + buffer.duration;
}

function setAudio(on) {
  state.audio = on;
  const btn = $("#btn-audio");
  btn.setAttribute("aria-pressed", on);
  btn.setAttribute("aria-label", on ? "Turn sound off" : "Turn sound on");
  if (on) {
    audioCtx ??= new AudioContext();
    audioCtx.resume();
    audioNext = 0;
  } else if (audioCtx) {
    audioCtx.close();
    audioCtx = null;
  }
  if (state.live?.ws.readyState === WebSocket.OPEN) state.live.ws.send(JSON.stringify({ audio: on }));
}

function stopLive(message = "") {
  if (state.live) {
    const live = state.live;
    state.live = null;
    live.ws.close();
    try { live.close(); } catch {}
  }
  if (state.audio) setAudio(false);
  showPtz(false);
  $("#live-toolbar").hidden = true;
  $("#btn-play").hidden = false;
  $("#live-stats").textContent = "";
  $("#live-msg").hidden = !message;
  $("#live-msg").textContent = message;
  if (document.fullscreenElement) document.exitFullscreen();
  setWindowFull(false);
}

// Toolbar fades out after a few seconds without the mouse moving over the video.
let idleTimer = null;
function wakeToolbar() {
  const wrap = $("#live-wrap");
  wrap.classList.remove("idle");
  clearTimeout(idleTimer);
  idleTimer = setTimeout(() => {
    if (state.live && !$("#live-toolbar").matches(":hover")) wrap.classList.add("idle");
  }, 3000);
}

// Real full screen where the browser allows it; otherwise (embedded browsers
// often refuse) fill the window with the video instead.
const isFull = () => !!document.fullscreenElement || $("#live-wrap").classList.contains("window-full");

function setWindowFull(on) {
  $("#live-wrap").classList.toggle("window-full", on);
  document.body.classList.toggle("no-scroll", on);
  updateFullButton();
}

function updateFullButton() {
  const full = isFull();
  const btn = $("#btn-full");
  btn.setAttribute("aria-label", full ? "Exit full screen" : "Full screen");
  btn.title = full ? "Exit full screen (Esc)" : "Full screen";
  btn.querySelector("path").setAttribute("d", full
    ? "M9 4v5H4M15 4v5h5M9 20v-5H4M15 20v-5h5"
    : "M4 9V4h5M20 9V4h-5M4 15v5h5M20 15v5h-5");
}

async function toggleFullscreen() {
  if (document.fullscreenElement) return document.exitFullscreen();
  if ($("#live-wrap").classList.contains("window-full")) return setWindowFull(false);
  try {
    if (!document.fullscreenEnabled) throw new Error("not allowed");
    // Some embedded browsers neither grant nor refuse the request, so don't wait forever.
    await Promise.race([
      $("#live-wrap").requestFullscreen(),
      new Promise((_, reject) => setTimeout(() => reject(new Error("timed out")), 500)),
    ]);
    if (!document.fullscreenElement) throw new Error("not granted");
  } catch {
    setWindowFull(true);
  }
}

function snapshot() {
  $("#live").toBlob((blob) => {
    const a = document.createElement("a");
    a.href = URL.createObjectURL(blob);
    a.download = `${state.current.name.replace(/\W+/g, "_")}_${new Date().toISOString().replace(/[:.]/g, "-")}.png`;
    a.click();
    setTimeout(() => URL.revokeObjectURL(a.href), 1000);
  }, "image/png");
}

// ── motion zones ───────────────────────────────────────────────────────────

async function openZones() {
  try {
    const z = await api(`/api/cameras/${state.current.id}/zones`);
    state.zones = z.grid;
  } catch (err) {
    return toast(`Couldn't read zones: ${err.message}`, true);
  }
  const grid = $("#zone-grid");
  grid.innerHTML = "";
  state.zones.forEach((row, r) => row.forEach((on, c) => {
    const cell = document.createElement("div");
    cell.dataset.r = r; cell.dataset.c = c;
    if (on) cell.classList.add("on");
    grid.append(cell);
  }));
  showPtz(false);
  grid.hidden = false;
  $("#zone-controls").hidden = false;
  if (!state.live) startLive();
  $("#live-wrap").scrollIntoView({ behavior: "smooth", block: "center" });
}

function closeZones(save) {
  const grid = $("#zone-grid");
  if (grid.hidden) return;
  if (save) {
    const out = state.zones.map((row) => row.map(() => 0));
    grid.querySelectorAll("div.on").forEach((d) => (out[d.dataset.r][d.dataset.c] = 1));
    api(`/api/cameras/${state.current.id}/zones`, { method: "PUT", body: { grid: out } })
      .then(() => toast("Motion zones saved"))
      .catch((err) => toast(`Not saved: ${err.message}`, true));
  }
  grid.hidden = true;
  $("#zone-controls").hidden = true;
}

function bindZones() {
  const grid = $("#zone-grid");
  let painting = null;
  grid.addEventListener("pointerdown", (e) => {
    if (!e.target.dataset.r) return;
    painting = !e.target.classList.contains("on");
    e.target.classList.toggle("on", painting);
    grid.setPointerCapture(e.pointerId);
  });
  grid.addEventListener("pointermove", (e) => {
    if (painting === null) return;
    const cell = document.elementFromPoint(e.clientX, e.clientY);
    if (cell?.parentElement === grid) cell.classList.toggle("on", painting);
  });
  grid.addEventListener("pointerup", () => (painting = null));
  $$("[data-zone]").forEach((b) => b.addEventListener("click", () => {
    const action = b.dataset.zone;
    if (action === "all" || action === "none") grid.querySelectorAll("div").forEach((d) => d.classList.toggle("on", action === "all"));
    else closeZones(action === "save");
  }));
}

// ── pan/tilt ───────────────────────────────────────────────────────────────

const camAction = (body) => api(`/api/cameras/${state.current.id}/action`, { method: "POST", body });

// Presets are parked (see kodax-web/attic/ptz_presets.md); the backend still supports them.

function showPtz(on) {
  $("#ptz-pad").hidden = !on;
  $("#btn-ptz").setAttribute("aria-pressed", on);
}

function bindPtz() {
  let holdTimer = null;
  let heldButton = null;
  const release = () => {
    if (holdTimer === null) return;
    clearInterval(holdTimer);
    holdTimer = null;
    heldButton?.classList.remove("held");
    heldButton = null;
    camAction({ action: "stop" }).catch(() => {});
  };
  // The camera moves while a direction is repeated (~every 0.5 s), then stops.
  // A hold is capped at 3 s (the server enforces this too) to protect the motor.
  const MAX_HOLD_MS = 3000;
  const hold = (direction, button) => {
    if (holdTimer !== null) return;
    heldButton = button;
    button?.classList.add("held");
    const started = performance.now();
    const nudge = () => {
      if (performance.now() - started > MAX_HOLD_MS) {
        toast("Stopped after 3 s to protect the motor. Press again to keep moving.");
        return release();
      }
      camAction({ action: "move", direction }).catch((err) => { toast(err.message, true); release(); });
    };
    nudge();
    holdTimer = setInterval(nudge, 500);
  };
  $$("#ptz-pad [data-move]").forEach((b) => {
    b.addEventListener("pointerdown", (e) => {
      b.setPointerCapture(e.pointerId);
      hold(b.dataset.move, b);
    });
    b.addEventListener("pointerup", release);
    b.addEventListener("pointercancel", release);
    b.addEventListener("contextmenu", (e) => e.preventDefault()); // long-press on touch
  });
  $("#btn-ptz").onclick = () => showPtz($("#ptz-pad").hidden);
  $("#btn-ptz-close").onclick = () => showPtz(false);

  const keys = { ArrowUp: "up", ArrowDown: "down", ArrowLeft: "left", ArrowRight: "right" };
  document.addEventListener("keydown", (e) => {
    if (!keys[e.key] || $("#ptz-pad").hidden || e.target.closest("input, select, textarea")) return;
    e.preventDefault();
    hold(keys[e.key], $(`#ptz-pad [data-move="${keys[e.key]}"]`));
  });
  document.addEventListener("keyup", (e) => { if (keys[e.key]) release(); });
  window.addEventListener("blur", release);

  const calibrate = async () => {
    if (!confirm("Calibrate pan & tilt? The camera sweeps to each end to find its limits (you may hear it briefly at the ends), then returns to centre. Takes about a minute.")) return;
    try {
      await camAction({ action: "calibrate" });
      toast("Calibrating… this takes about a minute");
      setTimeout(refreshStatus, 60000);
    } catch (err) { toast(err.message, true); }
  };
  $("#btn-calibrate").onclick = calibrate;
  $("#btn-calibrate-pad").onclick = calibrate;
}

// ── wifi, timezone, device actions ─────────────────────────────────────────

function bindActions() {
  $("#btn-play").onclick = startLive;
  $("#btn-stop").onclick = () => stopLive();
  $("#btn-audio").onclick = () => setAudio(!state.audio);
  $("#btn-night").onclick = cycleNightMode;
  $("#btn-snap").onclick = snapshot;
  $("#btn-full").onclick = toggleFullscreen;
  document.addEventListener("fullscreenchange", updateFullButton);
  document.addEventListener("keydown", (e) => {
    if (e.key === "Escape" && $("#live-wrap").classList.contains("window-full")) setWindowFull(false);
  });
  $("#live-wrap").addEventListener("pointermove", wakeToolbar);
  $("#live-wrap").addEventListener("dblclick", (e) => { if (e.target === $("#live")) toggleFullscreen(); });
  $("#btn-zones").onclick = openZones;
  $("#btn-refresh").onclick = refreshStatus;

  $("#cam-name").onclick = async () => {
    const name = prompt("Camera name", state.current.name);
    if (!name) return;
    await api(`/api/cameras/${state.current.id}`, { method: "PATCH", body: { name } });
    loadCameras(state.current.id);
  };
  $("#btn-remove").onclick = async () => {
    if (!confirm(`Remove ${state.current.name} from this app? The camera itself isn't changed.`)) return;
    await api(`/api/cameras/${state.current.id}`, { method: "DELETE" });
    state.current = null;
    loadCameras();
  };

  const showWifiForm = (on) => {
    $("#wifi-form").hidden = !on;
    $("#btn-wifi-change").hidden = on;
    if (!on) $("#wifi-form").reset();
  };
  $("#btn-wifi-change").onclick = () => showWifiForm(true);
  $("#btn-wifi-cancel").onclick = () => showWifiForm(false);
  $("#wifi-form").onsubmit = async (e) => {
    e.preventDefault();
    const form = new FormData(e.target);
    const ssid = form.get("ssid");
    if (!confirm(`Move ${state.current.name} to "${ssid}"? It will disconnect from ${state.status?.ssid || "its current network"} immediately.`)) return;
    try {
      await api(`/api/cameras/${state.current.id}/wifi`, { method: "POST", body: { ssid, password: form.get("password") } });
      toast("Wi-Fi details sent. The camera is reconnecting; update its IP here if it changes.");
      showWifiForm(false);
    } catch (err) {
      toast(`Wi-Fi change failed: ${err.message}`, true);
    }
  };

  const tz = $("#tz-select");
  const zones = Intl.supportedValuesOf ? Intl.supportedValuesOf("timeZone") : ["UTC"];
  const local = Intl.DateTimeFormat().resolvedOptions().timeZone;
  tz.innerHTML = zones.map((z) => `<option ${z === local ? "selected" : ""}>${z}</option>`).join("");
  tz.onchange = () => saveSetting("timezone", tz.value);

  $$("[data-melody]").forEach((b) => b.addEventListener("click", async () => {
    const stop = b.dataset.melody === "stop";
    try {
      await api(`/api/cameras/${state.current.id}/action`, {
        method: "POST", body: stop ? { action: "melody_stop" } : { action: "melody", track: Number(b.dataset.melody) } });
      toast(stop ? "Stopped" : `Playing track ${b.dataset.melody}`);
    } catch (err) { toast(err.message, true); }
  }));

  $("#btn-log").onclick = async () => {
    const pre = $("#setup-log");
    const { log } = await api(`/api/cameras/${state.current.id}/action`, { method: "POST", body: { action: "setup_log" } });
    try { pre.textContent = JSON.stringify(JSON.parse(log), null, 2); } catch { pre.textContent = log; }
    pre.hidden = false;
  };
  $("#btn-restart").onclick = async () => {
    if (!confirm(`Restart ${state.current.name}? It will be offline for about a minute.`)) return;
    stopLive();
    await api(`/api/cameras/${state.current.id}/action`, { method: "POST", body: { action: "restart" } });
    toast("Restarting…");
  };
}

// ── add camera ─────────────────────────────────────────────────────────────

function bindAdd() {
  const dlg = $("#dlg-add");
  $("#btn-add").onclick = () => { $("#add-error").textContent = ""; $("#add-form").reset(); dlg.showModal(); };
  $("#add-form").addEventListener("submit", async (e) => {
    if (e.submitter?.value !== "ok") return;
    e.preventDefault();
    const form = new FormData(e.target);
    $("#add-error").textContent = "Checking camera…";
    try {
      const cam = await api("/api/cameras", { method: "POST", body: { ip: form.get("ip"), name: form.get("name") } });
      dlg.close();
      loadCameras(cam.id);
    } catch (err) {
      $("#add-error").textContent = err.message;
    }
  });
}

// ── setup wizard ───────────────────────────────────────────────────────────

function bindSetup() {
  let found = null;
  $("#btn-setup").onclick = () => {
    stopLive();
    state.current = null;
    $$("#camera-list button").forEach((b) => b.setAttribute("aria-current", false));
    show("view-setup");
  };
  $("#btn-find").onclick = async () => {
    $("#find-result").textContent = "Looking for a camera in pairing mode…";
    try {
      const { camera } = await api("/api/setup/find");
      found = camera;
      $("#find-result").innerHTML = camera
        ? `Found camera <b>${esc(camera.mac)}</b> (firmware ${esc(camera.firmware)}) at ${esc(camera.ip)}.`
        : "No camera found. Check this PC is connected to the KodakCam-… network.";
      $("#setup-form-card").hidden = !camera;
    } catch (err) {
      $("#find-result").textContent = err.message;
    }
  };
  $("#setup-form").onsubmit = async (e) => {
    e.preventDefault();
    const form = new FormData(e.target);
    const steps = $("#setup-steps");
    steps.innerHTML = "";
    $("#setup-done").hidden = true;
    const body = {
      ip: found.ip,
      ssid: form.get("ssid"),
      password: form.get("password"),
      security: form.get("security"),
      flicker: Number(form.get("flicker")),
      timezone: Intl.DateTimeFormat().resolvedOptions().timeZone,
      utc_offset_minutes: -new Date().getTimezoneOffset(),
    };
    const resp = await fetch("/api/setup/run", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) });
    const reader = resp.body.pipeThrough(new TextDecoderStream()).getReader();
    let buffer = "";
    let last = null;
    for (;;) {
      const { value, done } = await reader.read();
      if (done) break;
      buffer += value;
      let nl;
      while ((nl = buffer.indexOf("\n")) >= 0) {
        const step = JSON.parse(buffer.slice(0, nl));
        buffer = buffer.slice(nl + 1);
        last = step;
        const li = document.createElement("li");
        li.className = step.ok ? "ok" : step.optional ? "skip" : "fail";
        li.textContent = `${step.step}: ${step.ok ? "done" : step.optional ? "not supported (skipped)" : `failed (${step.detail})`}`;
        steps.append(li);
      }
    }
    const done = $("#setup-done");
    done.hidden = false;
    done.innerHTML = last?.ok && last.step === "Send Wi-Fi details"
      ? `The camera is joining <b>${esc(body.ssid)}</b>. Reconnect this PC to your home Wi-Fi, find the camera's new IP in your router, then use <b>Add camera</b>.`
      : "Setup didn't finish. Check the steps above, put the camera back in pairing mode and try again.";
  };
}

// ── boot ───────────────────────────────────────────────────────────────────

bindSettings();
bindZones();
bindPtz();
bindActions();
bindAdd();
bindSetup();
loadCameras().catch((err) => toast(err.message, true));
