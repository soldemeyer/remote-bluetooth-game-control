/* RBGC server web GUI.
 *
 * Status arrives over a WebSocket at 10 Hz. Rendering is **incremental**: cards
 * are created once, keyed by bd_addr / client_id, and thereafter only changed
 * text and values are written. This is not a micro-optimisation -- it is a
 * correctness requirement.
 *
 * The previous version rebuilt both containers with innerHTML on every message.
 * At 100 ms intervals that produced two bugs with one cause:
 *
 *   * An open <select> was destroyed and recreated out from under the operator,
 *     so the Emulate dropdown closed the instant it was opened.
 *   * A click needs mousedown *and* mouseup on the same node. The node was
 *     routinely replaced between the two, so "Connection mode" silently did
 *     nothing.
 *
 * Two rules keep that from coming back:
 *
 *   1. Never replace a node that is still valid -- update it in place.
 *   2. Never write to a control the operator is interacting with (focused, or
 *      while a pointer is down anywhere on the page).
 *
 * Handlers are attached once per container by delegation, so they survive the
 * rebuilds that do happen when the adapter or client *set* changes.
 */

'use strict';

const $ = (id) => document.getElementById(id);

let socket = null;
let latest = null;
let bannerTimer = null;

/* True while a mouse button or touch is held anywhere. A redraw during a click
 * is what ate the button presses, so we suspend updates for the duration. */
let pointerDown = false;
addEventListener('pointerdown', () => { pointerDown = true; }, true);
addEventListener('pointerup', () => { pointerDown = false; }, true);
addEventListener('pointercancel', () => { pointerDown = false; }, true);

/** Should this control be left alone right now? */
function busy(element) {
  return pointerDown || document.activeElement === element;
}

/** Write text only when it actually changed, to avoid pointless layout work. */
function setText(element, text) {
  if (element && element.textContent !== text) element.textContent = text;
}

/** Write HTML only when it actually changed. */
function setHtml(element, html) {
  if (element && element.innerHTML !== html) element.innerHTML = html;
}

/* ---------- auth ---------- */

$('login-form').addEventListener('submit', async (event) => {
  event.preventDefault();
  const error = $('login-error');
  error.textContent = '';

  try {
    const response = await fetch('/api/login', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ password: $('password').value }),
    });

    if (!response.ok) {
      const body = await response.json().catch(() => ({}));
      error.textContent = body.error || 'Sign-in failed';
      return;
    }

    $('login').classList.add('hidden');
    $('app').classList.remove('hidden');
    connect();
  } catch (exc) {
    error.textContent = 'Could not reach the server.';
  }
});

$('logout').addEventListener('click', async () => {
  await fetch('/api/logout', { method: 'POST' });
  location.reload();
});

/* ---------- websocket ---------- */

function connect() {
  const scheme = location.protocol === 'https:' ? 'wss' : 'ws';
  socket = new WebSocket(`${scheme}://${location.host}/ws`);

  socket.onmessage = (event) => {
    const message = JSON.parse(event.data);
    if (message.type === 'status') {
      latest = message.data;
      render(latest);
    }
  };

  socket.onclose = () => {
    // Reconnect rather than silently going stale -- an operator staring at a
    // frozen page during a game would have no idea anything was wrong.
    showBanner('Connection to the server lost. Reconnecting...', 'error', 0);
    setTimeout(connect, 2000);
  };
}

/* ---------- API helpers ---------- */

async function post(path, body) {
  try {
    const response = await fetch(path, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(body || {}),
    });
    const data = await response.json().catch(() => ({}));

    if (!response.ok) {
      showBanner(data.error || data.message || 'Request failed', 'error');
      return null;
    }
    if (data.message) showBanner(data.message, 'good');
    return data;
  } catch (exc) {
    showBanner('Could not reach the server.', 'error');
    return null;
  }
}

function showBanner(text, kind, timeout = 5000) {
  const banner = $('banner');
  banner.textContent = text;
  banner.className = `banner ${kind || ''}`;
  banner.classList.remove('hidden');

  clearTimeout(bannerTimer);
  if (timeout > 0) {
    bannerTimer = setTimeout(() => banner.classList.add('hidden'), timeout);
  }
}

/* ---------- rendering ---------- */

function render(status) {
  setText($('server-name'), status.server.name);
  setText($('server-sub'),
    `UDP port ${status.server.client_port} · capacity ` +
    `${status.server.capacity} controller${status.server.capacity === 1 ? '' : 's'}`);

  const autoApprove = $('auto-approve');
  if (!busy(autoApprove)) autoApprove.checked = status.server.auto_approve;

  const rumble = $('rumble-enabled');
  if (!busy(rumble)) rumble.checked = status.server.rumble_enabled;

  renderServerPanel(status);
  renderIdentity(status);
  renderAdapters(status);
  renderClients(status);
  renderVideo(status.video);
  renderDatapath(status.datapath);
}

/* ---------- video ----------
 *
 * Same discipline as everywhere else in this file: values are written in
 * place, never by rebuilding the section, and never into a control the
 * operator is currently touching. The device dropdowns get a signature so
 * their options are only rebuilt when the list genuinely changed -- otherwise
 * an open dropdown would close ten times a second.
 */

function renderVideo(video) {
  const section = $('video-section');
  if (!video) {
    if (section) section.classList.add('hidden');
    return;
  }
  if (section) section.classList.remove('hidden');

  const state = $('video-state');
  if (state) {
    let label = 'Off';
    let kind = 'pill';
    if (video.mode !== 'off') {
      if (video.live) { label = 'Streaming'; kind = 'pill approved'; }
      else if (video.connected) { label = 'Connected'; kind = 'pill approved'; }
      else { label = 'Waiting for a source'; kind = 'pill pending'; }
    }
    setText(state, label);
    state.className = kind;
  }

  document.querySelectorAll('input[name="video-mode"]').forEach((radio) => {
    if (!busy(radio)) radio.checked = radio.value === video.mode;
  });

  const status = video.status || {};
  setText($('video-status'), describeVideoStatus(video));
  setText($('video-encoder'), status.encoder || '—');
  setText($('video-rate'),
    status.streaming
      ? `${status.width}×${status.height} · ${Math.round(status.fps || 0)} fps · ` +
        `${status.bitrate_kbps || 0} kbps${status.relay_capped ? ' (capped for relay)' : ''}`
      : '—');
  setText($('video-clients'), status.clients === undefined ? '—' : String(status.clients));

  renderAudioMeter(status);

  const errors = $('video-errors');
  if (errors) {
    const list = status.errors || [];
    setText(errors, list.length ? list[list.length - 1] : '');
  }

  renderVideoConnection(video);
  renderVideoConfig(video);
  renderVideoCaps(video);
}

/* Capture level.
 *
 * "Audio: on" only means a thread is alive -- a muted input, the wrong capture
 * channel, or a console with its volume down all satisfy it while sending
 * silence. This is the only readout where working and broken look different,
 * so it is worth the few lines. Same three states as the video server's own
 * meter: off, live-but-silent, and a level. */

function renderAudioMeter(status) {
  const row = $('video-audio-row');
  if (!row) return;

  const bar = $('video-audio-bar');
  const peak = $('video-audio-peak');
  const label = $('video-audio-label');

  if (!status.audio) {
    row.classList.add('muted-row');
    if (bar) bar.style.width = '0%';
    if (peak) peak.style.left = '0%';
    setText(label, 'off');
    return;
  }
  row.classList.remove('muted-row');

  if (!status.audio_live) {
    if (bar) bar.style.width = '0%';
    if (peak) peak.style.left = '0%';
    setText(label, 'no audio');
    return;
  }

  const rms = Math.max(0, Math.min(Number(status.audio_rms) || 0, 1));
  const top = Math.max(0, Math.min(Number(status.audio_level) || 0, 1));
  if (bar) bar.style.width = `${(rms * 100).toFixed(1)}%`;
  if (peak) peak.style.left = `${(top * 100).toFixed(1)}%`;
  setText(label, rms > 0.001 ? `${Math.round(rms * 100)}%` : 'silent');
}

/* The address, port and password we use to reach the video server.
 *
 * Hidden in embedded mode: the video server is this machine's own subprocess,
 * so there is nothing for the operator to point at or authenticate to -- the
 * server generates that password itself. */

function renderVideoConnection(video) {
  const panel = $('video-connection');
  if (!panel) return;

  if (video.mode === 'embedded' || video.mode === 'off') {
    panel.classList.add('hidden');
    return;
  }
  panel.classList.remove('hidden');

  const connection = video.connection || {};

  const host = $('video-host');
  if (host && !busy(host)) host.value = connection.host || '';

  const port = $('video-port');
  if (port && !busy(port)) port.value = connection.port || 47810;

  const hint = $('video-password-hint');
  if (hint) {
    const link = connection.link || {};
    if (!connection.host) {
      setText(hint, 'Detect a video server, or type its address.');
    } else if (!connection.has_password) {
      setText(hint, 'Enter the password shown on the video server.');
    } else if (link.connected) {
      setText(hint, `Connected to ${connection.host}:${connection.port}.`);
    } else {
      setText(hint, link.last_error || 'Connecting…');
    }
  }
}

/* Detection results. Kept in a signature-guarded rebuild like every other
 * dropdown here, so choosing one is not interrupted by the 10 Hz refresh. */

function renderDetectedServers(servers) {
  const select = $('video-found');
  if (!select) return;

  const signature = servers.map((s) => `${s.host}:${s.port}`).join('|');
  select.dataset.signature = signature;

  const options = ['<option value="">Enter an address manually</option>'];
  for (const found of servers) {
    const detail = found.streaming
      ? `${found.width}×${found.height}`
      : 'idle';
    options.push(
      `<option value="${escapeHtml(found.host)}:${found.port}">` +
      `${escapeHtml(found.name)} — ${escapeHtml(found.host)} (${detail})</option>`,
    );
  }
  select.innerHTML = options.join('');
  applyDetectedSelection();
}

/* A detected server and a typed address are alternatives, not a form to fill in
 * twice. Picking one from the dropdown fills the address fields and locks them,
 * so there is never a question of which of the two is actually being used --
 * the fields still carry the value, and a disabled input keeps its value, so
 * the connect handler needs no special case.
 *
 * Selecting the blank entry hands the fields back. */
function applyDetectedSelection() {
  const select = $('video-found');
  const host = $('video-host');
  const port = $('video-port');
  if (!select || !host || !port) return;

  const chosen = select.value;
  if (chosen) {
    const separator = chosen.lastIndexOf(':');
    host.value = chosen.slice(0, separator);
    port.value = chosen.slice(separator + 1);
  }

  host.disabled = !!chosen;
  port.disabled = !!chosen;
  const hint = $('video-address-hint');
  if (hint) {
    setText(hint, chosen
      ? 'Using the detected server above.'
      : 'Enter the address of the video server.');
  }
}

function describeVideoStatus(video) {
  if (video.mode === 'off') return 'Off';
  const embedded = video.embedded || {};
  if (video.mode === 'embedded' && !video.connected) {
    if (embedded.error) return `Failed: ${embedded.error}`;
    if (embedded.running) return 'Starting…';
    return 'Not running';
  }
  if (!video.connected) return 'Waiting for a video server to connect';
  if (video.stale) return 'Connected, but not reporting';
  if (video.config_pending) return 'Applying settings…';
  return `Streaming from ${video.source || 'the source'}`;
}

function renderVideoConfig(video) {
  const settings = video.settings || {};

  fillDeviceSelect($('video-device'), video.devices, 'video', settings.device);
  fillDeviceSelect($('video-audio-device'), video.devices, 'audio', settings.audio_device);

  const resolution = $('video-resolution');
  if (resolution && !busy(resolution)) {
    resolution.value = `${settings.width}x${settings.height}`;
  }
  const fps = $('video-fps');
  if (fps && !busy(fps)) fps.value = String(settings.fps);

  const bitrate = $('video-bitrate');
  if (bitrate && !busy(bitrate)) bitrate.value = settings.bitrate_kbps;

  const previewWidth = $('video-preview-width');
  if (previewWidth && !busy(previewWidth)) {
    previewWidth.value = String(settings.preview_width);
  }
  const previewFps = $('video-preview-fps');
  if (previewFps && !busy(previewFps)) previewFps.value = String(settings.preview_fps);
  setPreviewRate(settings.preview_fps);

  const audio = $('video-audio-enabled');
  if (audio && !busy(audio)) audio.checked = !!settings.audio_enabled;

  const test = $('video-test-source');
  if (test && !busy(test)) test.checked = !!settings.test_source;
}

function fillDeviceSelect(select, devices, kind, current) {
  if (!select) return;
  const list = (devices || []).filter((d) => d.kind === kind);
  const signature = list.map((d) => d.id).join('|');
  if (select.dataset.signature !== signature) {
    if (busy(select)) return;
    select.dataset.signature = signature;
    const options = ['<option value="">First available</option>'];
    for (const device of list) {
      options.push(`<option value="${escapeHtml(device.id)}">${escapeHtml(device.name)}</option>`);
    }
    select.innerHTML = options.join('');
  }
  if (!busy(select) && current !== undefined) select.value = current || '';
}

function renderVideoCaps(video) {
  const hint = $('video-caps-hint');
  if (!hint) return;
  if (video.mode !== 'embedded') {
    setText(hint, '');
    return;
  }
  const caps = video.embedded_caps || {};
  setText(hint,
    `Running here, so encoding is done by this machine's CPU: limited to ` +
    `${caps.width}×${caps.height}, ${caps.fps} fps, ${caps.bitrate_kbps} kbps.`);
}

/* ---------- video preview ----------
 *
 * Polled as an ordinary authenticated fetch and swapped in as a blob, rather
 * than pointed at a URL: an empty response then leaves the previous frame on
 * screen instead of flashing a broken image every time one is missed.
 */

let previewTimer = null;
let previewUrl = null;
//: Poll interval, from the configured preview frame rate. Fixed at 200 ms it
//: capped the picture at 5 fps however fast the source was told to send, so
//: raising the setting appeared to do nothing at all.
let previewIntervalMs = 100;

function setPreviewRate(fps) {
  const wanted = Math.max(33, Math.round(1000 / Math.max(Number(fps) || 10, 1)));
  if (wanted === previewIntervalMs) return;
  previewIntervalMs = wanted;
  // Re-arm at the new rate if it is already running.
  if (previewTimer) {
    clearInterval(previewTimer);
    previewTimer = setInterval(fetchPreview, previewIntervalMs);
  }
}

function startPreview() {
  if (previewTimer) return;
  // Stays hidden until a frame actually lands; the source can take a couple of
  // seconds to be told that somebody is watching.
  const img = $('video-preview-img');
  if (img && !img.getAttribute('src')) img.classList.add('hidden');
  const toggle = $('video-preview-toggle');
  if (toggle) setText(toggle, 'Hide');
  setText($('video-preview-hint'), 'Waiting for a frame…');
  previewTimer = setInterval(fetchPreview, previewIntervalMs);
  fetchPreview();
}

function stopPreview() {
  if (previewTimer) {
    clearInterval(previewTimer);
    previewTimer = null;
  }
  const toggle = $('video-preview-toggle');
  if (toggle) setText(toggle, 'Show');

  // Hidden, not merely blanked. An <img> with no src renders as a broken-image
  // icon with its alt text beside it, which reads as a failure rather than as
  // "nothing here yet" -- and it is what an operator sees every time the panel
  // is closed.
  const img = $('video-preview-img');
  if (img) {
    img.removeAttribute('src');
    img.classList.add('hidden');
  }
  if (previewUrl) {
    URL.revokeObjectURL(previewUrl);
    previewUrl = null;
  }
  setText($('video-preview-hint'), 'Preview is off.');
}

async function fetchPreview() {
  try {
    const response = await fetch('/api/video/preview', { cache: 'no-store' });
    if (response.status === 204) {
      setText($('video-preview-hint'), 'No picture yet.');
      return;
    }
    if (!response.ok) return;

    const blob = await response.blob();
    if (!blob.size) return;
    const url = URL.createObjectURL(blob);
    const img = $('video-preview-img');
    if (img) {
      img.src = url;
      img.classList.remove('hidden');
    }
    // Revoke only after the new one is in place, so there is no blank frame
    // between the two.
    if (previewUrl) URL.revokeObjectURL(previewUrl);
    previewUrl = url;
    setText($('video-preview-hint'), '');
  } catch (exc) {
    /* transient; the next tick tries again */
  }
}

/* ---------- server control panel ---------- */

function renderServerPanel(status) {
  const server = status.server || {};

  const lan = $('server-lan-enabled');
  if (lan && !busy(lan)) lan.checked = !!server.lan_enabled;

  const internet = $('server-internet-enabled');
  if (internet && !busy(internet)) internet.checked = !!server.internet_enabled;

  const state = $('server-state');
  if (state) {
    const paths = [];
    if (server.lan_enabled) paths.push('LAN');
    if (server.internet_enabled) paths.push('Internet');
    setText(state, paths.length ? `Accepting: ${paths.join(' + ')}` : 'Not accepting clients');
    state.className = paths.length ? 'pill approved' : 'pill pending';
  }

  // Internet needs a broker configured at startup; saying so beats a toggle
  // that flips on and quietly does nothing.
  const note = $('server-internet-note');
  if (note) {
    setText(note, server.broker_ready
      ? 'Clients introduced by the rendezvous broker.'
      : 'No broker configured or reachable — set one below, then restart the server.');
  }

  // Text inputs are only seeded when untouched, so typing is never clobbered.
  const name = $('server-name-input');
  if (name && !busy(name) && name.value === '') name.value = server.name || '';

  const lanVisibility = $('server-lan-visibility');
  if (lanVisibility && !busy(lanVisibility)) {
    lanVisibility.value = server.lan_discoverable ? 'visible' : 'hidden';
  }

  const netVisibility = $('server-internet-visibility');
  if (netVisibility && !busy(netVisibility)) {
    netVisibility.value = server.internet_discoverable ? 'visible' : 'hidden';
  }

  const stunField = $('server-stun');
  if (stunField && !busy(stunField)) {
    stunField.value = (server.stun_servers || []).join(', ');
  }

  const broker = $('server-broker');
  if (broker && !busy(broker) && broker.value === '') {
    broker.value = server.broker || '';
  }
}

/* ---------- adapters ---------- */

/* What the console sees, in two halves: the report layout it receives, and who
 * we claim to be. Both are rebuilt only when their list changes, and never
 * written to while the operator has the dropdown open -- the same rule as every
 * other control in this file. */

/* The emulated controller -- the report layout the console receives.
 *
 * Server-wide, and shown here rather than on each adapter card. It used to be
 * per adapter, which could not work: BlueZ publishes one HID service record per
 * machine, so an adapter set to a different profile sent reports in a format
 * the console had never been told to expect. */
function renderProfileChoice(status) {
  const select = $('bt-profile');
  if (!select) return;

  const profiles = status.profiles || [];
  const signature = profiles.map((p) => p.name).join(',');
  if (select.dataset.signature !== signature) {
    if (busy(select)) return;
    select.dataset.signature = signature;
    select.innerHTML = profiles
      .map((p) => `<option value="${escapeHtml(p.name)}">${escapeHtml(p.display_name)}</option>`)
      .join('');
  }

  // Whatever the adapters are actually running. They are all the same by
  // construction now, so the first one speaks for all of them.
  const current = (status.adapters || []).map((a) => a.profile).find(Boolean);
  if (!busy(select) && current && select.value !== current) select.value = current;
}

/* Who the adapters claim to be: advertised name, and the vendor and product ids
 * in the DeviceID record. */
function renderIdentity(status) {
  renderProfileChoice(status);

  const select = $('bt-identity');
  if (!select) return;

  const identities = status.identities || [];
  const signature = identities.map((i) => i.key).join('|');
  if (select.dataset.signature !== signature) {
    if (busy(select)) return;
    select.dataset.signature = signature;
    select.innerHTML = identities
      .map((i) => `<option value="${escapeHtml(i.key)}">${escapeHtml(i.name)}</option>`)
      .join('');
  }

  if (!busy(select)) select.value = status.identity || 'generic';

  const chosen = identities.find((i) => i.key === select.value);
  setText($('bt-identity-note'),
    chosen ? `${chosen.device_name} · ${chosen.vendor} — ${chosen.note}` : '');
}

function renderAdapters(status) {
  const container = $('adapters');
  const hardware = status.hardware || [];
  const channels = status.adapters || [];

  setText($('adapter-count'),
    hardware.length ? `${channels.length} of ${hardware.length} enabled` : `${channels.length}`);

  // Mock mode reports no hardware, so fall back to rendering channels directly.
  const rows = hardware.length ? hardware : channels.map((c) => ({
    bd_addr: c.bd_addr, hci: c.hci, manufacturer: '', enabled: true, up: true,
  }));

  if (!rows.length) {
    setHtml(container,
      '<div class="empty">No Bluetooth adapters detected. Plug in a dongle and click Rescan.</div>');
    container.dataset.keys = '';
    return;
  }

  // Rebuild only when the *set* of adapters changes -- not on every tick.
  const keys = rows.map((hw) => hw.bd_addr).join(',');
  if (container.dataset.keys !== keys) {
    container.innerHTML = rows.map((hw) => adapterCardSkeleton(hw)).join('');
    container.dataset.keys = keys;
  }

  rows.forEach((hw) => {
    const channel = channels.find((c) => c.bd_addr === hw.bd_addr);
    updateAdapterCard(container, hw, channel, status);
  });
}

/** Static structure for one adapter. Filled in by updateAdapterCard(). */
function adapterCardSkeleton(hw) {
  // The title is the name the player sees on their console ("RBGC Gamepad 2").
  // hciX and the BD_ADDR are diagnostics and belong underneath: hciX in
  // particular reshuffles across reboots, so leading with it named the card
  // after the least stable thing about it.
  return `
    <div class="card" data-card="${hw.bd_addr}">
      <div class="card-head">
        <div>
          <div class="card-title">
            <span class="status-dot" data-field="dot"></span><span data-field="name"></span>
          </div>
          <div class="mono muted"><span data-field="hci"></span> &middot; ${hw.bd_addr}</div>
          <div class="muted" data-field="manufacturer"></div>
        </div>
        <label class="toggle">
          <input type="checkbox" data-action="enable" data-addr="${hw.bd_addr}">
          <span>On</span>
        </label>
      </div>

      <div class="muted" data-field="state"></div>
      <div class="pairing hidden" data-field="pairing"></div>
      <div class="degraded hidden" data-field="hid-error"></div>

      <div data-field="body" class="hidden">
        <div data-field="assignment"></div>
        <div data-field="write-stats"></div>
        <div class="card-row">
          <button class="small" data-action="pair" data-addr="${hw.bd_addr}">Connection mode</button>
          <button class="secondary small" data-action="unpair" data-addr="${hw.bd_addr}">Stop</button>
        </div>
      </div>
    </div>`;
}

function updateAdapterCard(container, hw, channel, status) {
  const card = container.querySelector(`[data-card="${hw.bd_addr}"]`);
  if (!card) return;

  const field = (name) => card.querySelector(`[data-field="${name}"]`);

  const enabled = !!channel;
  const connected = channel && channel.connected;
  const ready = channel && channel.ready;

  // A HID bind failure outranks every other state. The adapter still appears
  // and keeps its name -- deliberately, since a vanished adapter is harder to
  // diagnose -- but saying "Waiting for console" would be a lie: it cannot
  // serve one, and a host that finds it will pair and then fail.
  const hidError = hw.hid_error || '';

  let dot = 'off';
  let stateText = 'Disabled';
  if (hidError) { dot = 'off'; stateText = 'Not serving HID'; }
  else if (enabled && connected && ready) { dot = 'live'; stateText = 'Connected'; }
  else if (enabled && connected) { dot = 'waiting'; stateText = 'Connected, handshaking'; }
  else if (enabled) { dot = 'waiting'; stateText = 'Waiting for console'; }

  card.classList.toggle('disabled', !enabled);
  field('dot').className = `status-dot ${dot}`;
  setText(field('name'), hw.name || (hw.number ? `RBGC Gamepad ${hw.number}` : hw.hci));
  setText(field('hci'), hw.hci);
  setText(field('manufacturer'), hw.manufacturer || '');
  setText(field('state'), stateText);

  // Pairing is a timed state with no feedback from the Bluetooth stack, so
  // say so explicitly and count down -- otherwise the operator presses
  // "Connection mode" and nothing visibly happens.
  const pairing = field('pairing');
  const remaining = hw.pairing_s || 0;
  pairing.classList.toggle('hidden', remaining <= 0 || !!hidError);
  if (remaining > 0) {
    setText(pairing, `Waiting for a console to connect… ${remaining}s left`);
  }

  const failure = field('hid-error');
  failure.classList.toggle('hidden', !hidError);
  if (hidError) {
    setText(failure, `Bluetooth HID did not start — ${hidError}`);
  }

  const toggle = card.querySelector('[data-action="enable"]');
  if (!busy(toggle)) toggle.checked = enabled;

  field('body').classList.toggle('hidden', !enabled);
  if (!enabled) return;

  setHtml(field('assignment'), channel.assigned_client
    ? `<div class="assigned-to">
         <strong>${escapeHtml(channel.username || 'unnamed')}</strong>
         &middot; slot ${channel.assigned_slot}
         <button class="secondary small" style="float:right"
                 data-action="unassign" data-addr="${hw.bd_addr}">Unassign</button>
       </div>`
    : '<div class="assigned-to muted">No controller assigned</div>');

  setHtml(field('write-stats'), channel.write_ms && channel.write_ms.count
    ? `<div class="muted">BT write p50 ${channel.write_ms.p50} ms &middot;
        p99 ${channel.write_ms.p99} ms &middot; sent ${channel.reports_sent}</div>`
    : '');
}

/* ---------- clients ---------- */

function renderClients(status) {
  const container = $('clients');
  const clients = status.clients || [];
  setText($('client-count'), `${clients.length} of ${status.server.max_clients}`);

  if (!clients.length) {
    setHtml(container,
      '<div class="empty">No clients connected. Point a client at this server\'s address and port.</div>');
    container.dataset.keys = '';
    return;
  }

  // Rebuild when the set of clients, their slots, or the adapter list changes --
  // all three affect the structure rather than just the values.
  const keys = clients.map((c) =>
    `${c.client_id}:${c.state}:${(c.slots || []).map((s) => s.slot).join('.')}`
  ).join(',') + '|' + (status.adapters || []).map((a) => a.bd_addr).join('.');

  if (container.dataset.keys !== keys) {
    // Never restructure mid-interaction; the next tick is only 100 ms away.
    if (pointerDown || (document.activeElement && container.contains(document.activeElement))) {
      return;
    }
    container.innerHTML = clients.map((client) => clientCard(client, status)).join('');
    container.dataset.keys = keys;
  }

  clients.forEach((client) => updateClientCard(container, client, status));
}

function clientCard(client, status) {
  const pending = client.state === 'PENDING';

  const actions = pending
    ? `<button class="good small" data-action="approve" data-client="${client.client_id}">Approve</button>
       <button class="danger small" data-action="deny" data-client="${client.client_id}">Deny</button>`
    : `<button class="danger small" data-action="deny" data-client="${client.client_id}">Disconnect</button>`;

  const rows = (client.slots || []).map((slot) => `
      <tr data-slot-row="${client.client_id}-${slot.slot}">
        <td>${slot.slot}</td>
        <td data-field="username"></td>
        <td class="muted" data-field="device"></td>
        <td data-field="link"></td>
        <td data-field="latency"></td>
        <td>
          <select data-action="assign" data-client="${client.client_id}"
                  data-slot="${slot.slot}" ${pending ? 'disabled' : ''}></select>
        </td>
      </tr>`).join('');

  return `
    <div class="client ${pending ? 'pending' : ''}" data-client-card="${client.client_id}">
      <div class="client-head">
        <div>
          <strong>${escapeHtml(client.client_name || client.client_id.slice(0, 8))}</strong>
          <span class="pill ${pending ? 'pending' : 'approved'}">${client.state}</span>
          <span class="pill" data-field="rumble" title="This client's rumble setting"></span>
          <div class="mono muted">${escapeHtml(client.address)}</div>
        </div>
        <div class="card-row">${actions}</div>
      </div>
      ${rows ? `<table>
        <thead><tr>
          <th>Slot</th><th>Player</th><th>Device</th><th></th><th>Latency</th><th>Adapter</th>
        </tr></thead>
        <tbody>${rows}</tbody>
      </table>` : '<div class="muted">No controllers reported yet.</div>'}
    </div>`;
}

function updateClientCard(container, client, status) {
  const card = container.querySelector(`[data-client-card="${client.client_id}"]`);
  if (!card) return;

  setText(card.querySelector('[data-field="rumble"]'),
    `rumble ${client.rumble_enabled ? 'on' : 'off'}`);

  (client.slots || []).forEach((slot) => {
    const row = card.querySelector(`[data-slot-row="${client.client_id}-${slot.slot}"]`);
    if (!row) return;

    const field = (name) => row.querySelector(`[data-field="${name}"]`);
    setText(field('username'), slot.username || '—');
    setText(field('device'), slot.device_name || '—');
    setHtml(field('link'), slot.connected ? '' : '<span class="latency-bad">disconnected</span>');
    setHtml(field('latency'), latencyCell(slot.rtt_ms));

    const select = row.querySelector('[data-action="assign"]');
    if (busy(select)) return;

    const channel = (status.adapters || []).find(
      (c) => c.assigned_client === client.client_id && c.assigned_slot === slot.slot);

    const available = (status.adapters || []).filter(
      (c) => !c.assigned_client ||
             (c.assigned_client === client.client_id && c.assigned_slot === slot.slot));

    // Label by the name the player sees on their console, not by hciX --
    // the operator is matching "player 2" to "RBGC Gamepad 2", and hciX
    // reshuffles across reboots anyway.
    const signature = available.map((c) => `${c.bd_addr}:${adapterName(c)}`).join(',');
    if (select.dataset.signature !== signature) {
      select.innerHTML = ['<option value="">— not assigned —</option>'].concat(
        available.map((c) => `<option value="${c.bd_addr}">${escapeHtml(adapterName(c))}</option>`),
      ).join('');
      select.dataset.signature = signature;
    }

    const wanted = channel ? channel.bd_addr : '';
    if (select.value !== wanted) select.value = wanted;
  });
}

/** The name an adapter advertises, falling back sensibly. */
function adapterName(channel) {
  if (!latest) return channel.hci;
  const hardware = (latest.hardware || []).find((h) => h.bd_addr === channel.bd_addr);
  if (hardware && hardware.name) return hardware.name;
  if (hardware && hardware.number) return `RBGC Gamepad ${hardware.number}`;
  return channel.hci;
}

function latencyCell(stats) {
  if (!stats || !stats.count) return '<span class="muted">—</span>';

  // Thresholds reflect what is achievable, not the impossible 2-5 ms target:
  // the Bluetooth hop alone costs 5-15 ms. See CLAUDE.md.
  const p50 = stats.p50;
  const cls = p50 < 25 ? 'latency-good' : p50 < 60 ? 'latency-ok' : 'latency-bad';
  return `<span class="${cls}">${p50.toFixed(1)} ms</span>` +
         `<span class="muted"> / p99 ${stats.p99.toFixed(1)}</span>`;
}

/* ---------- delegated event handling ----------
 *
 * Bound once to the containers rather than to each control. Handlers therefore
 * survive the structural rebuilds above -- re-attaching per element on every
 * tick is what made clicks unreliable in the first place.
 */

function delegate(containerId, handler) {
  const container = $(containerId);
  container.addEventListener('click', (event) => {
    const element = event.target.closest('[data-action]');
    if (element && container.contains(element) && element.tagName !== 'SELECT') {
      handler(element);
    }
  });
  container.addEventListener('change', (event) => {
    const element = event.target.closest('[data-action]');
    if (element && container.contains(element)) handler(element);
  });
}

delegate('adapters', async (element) => {
  const action = element.dataset.action;
  const bd_addr = element.dataset.addr;

  if (action === 'enable') {
    await post('/api/adapter/enable', { bd_addr, enabled: element.checked });
  } else if (action === 'pair') {
    await post('/api/adapter/pair', { bd_addr, pairable: true, duration: 120 });
  } else if (action === 'unpair') {
    await post('/api/adapter/pair', { bd_addr, pairable: false });
  } else if (action === 'unassign') {
    await post('/api/assign', { bd_addr });
  }
});

delegate('clients', async (element) => {
  const action = element.dataset.action;
  const client_id = element.dataset.client;

  if (action === 'approve') {
    await post('/api/approve', { client_id });
  } else if (action === 'deny') {
    await post('/api/deny', { client_id });
  } else if (action === 'assign') {
    const bd_addr = element.value;
    const slot = parseInt(element.dataset.slot, 10);

    if (!bd_addr) {
      // Clear whichever adapter currently holds this slot.
      const current = (latest.adapters || []).find(
        (c) => c.assigned_client === client_id && c.assigned_slot === slot);
      if (current) await post('/api/assign', { bd_addr: current.bd_addr });
    } else {
      await post('/api/assign', { bd_addr, client_id, slot });
    }
  }
});

delegate('video-section', async (element) => {
  const action = element.dataset.action;

  if (action === 'video-mode') {
    await post('/api/video/mode', { mode: element.value });
  } else if (action === 'video-detect') {
    const data = await post('/api/video/detect', {});
    if (data) renderDetectedServers(data.servers || []);
  } else if (action === 'video-connect') {
    await post('/api/video/connection', {
      host: $('video-host').value.trim(),
      port: Number($('video-port').value) || 47810,
      password: $('video-password').value,
    });
    // Never leave a credential sitting in the form.
    $('video-password').value = '';
  } else if (action === 'video-probe') {
    await post('/api/video/probe', {});
  } else if (action === 'video-preview-toggle') {
    if (previewTimer) stopPreview(); else startPreview();
  }
});

$('video-found').addEventListener('change', applyDetectedSelection);

$('video-config-form').addEventListener('submit', async (event) => {
  event.preventDefault();
  const [width, height] = ($('video-resolution').value || '1280x720').split('x');
  await post('/api/video/config', {
    device: $('video-device').value,
    audio_device: $('video-audio-device').value,
    width: Number(width),
    height: Number(height),
    fps: Number($('video-fps').value),
    bitrate_kbps: Number($('video-bitrate').value),
    preview_width: Number($('video-preview-width').value),
    preview_fps: Number($('video-preview-fps').value),
    audio_enabled: $('video-audio-enabled').checked,
    test_source: $('video-test-source').checked,
  });
});

/* ---------- datapath ---------- */

function renderDatapath(stats) {
  if (!stats) return;

  const process = stats.process_ms || {};
  setHtml($('datapath'), `
    ${stat('Packets received', formatCount(stats.packets_received))}
    ${stat('Dropped', formatCount(stats.packets_dropped))}
    ${stat('Unroutable', formatCount(stats.packets_unroutable))}
    ${stat('Decrypt failures', formatCount(stats.decrypt_failures))}
    ${stat('Server process p50', process.count ? `${process.p50}<small> ms</small>` : '—')}
    ${stat('Server process p99', process.count ? `${process.p99}<small> ms</small>` : '—')}
  `);
}

function stat(label, value) {
  return `<div class="stat">
            <div class="stat-label">${label}</div>
            <div class="stat-value">${value}</div>
          </div>`;
}

function formatCount(value) {
  if (value === undefined || value === null) return '—';
  if (value < 1000) return String(value);
  if (value < 1e6) return `${(value / 1000).toFixed(1)}k`;
  return `${(value / 1e6).toFixed(2)}M`;
}

function escapeHtml(value) {
  return String(value === undefined || value === null ? '' : value).replace(
    /[&<>"']/g,
    (ch) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[ch]),
  );
}

/* ---------- header + server panel actions ---------- */

$('auto-approve').addEventListener('change', (event) => {
  post('/api/settings', { auto_approve: event.target.checked });
});

$('rumble-enabled').addEventListener('change', (event) => {
  post('/api/settings', { rumble_enabled: event.target.checked });
});

$('rescan').addEventListener('click', () => post('/api/rescan'));

/* Connection toggles apply on change -- no Save button. */
const lanEnabled = $('server-lan-enabled');
if (lanEnabled) {
  lanEnabled.addEventListener('change', (event) => {
    post('/api/server/state', { lan: event.target.checked });
  });
}

const internetEnabled = $('server-internet-enabled');
if (internetEnabled) {
  internetEnabled.addEventListener('change', (event) => {
    post('/api/server/state', { internet: event.target.checked });
  });
}

const identityForm = $('server-identity-form');
if (identityForm) {
  identityForm.addEventListener('submit', async (event) => {
    event.preventDefault();
    const body = { name: $('server-name-input').value };

    const password = $('server-password-input').value;
    if (password) body.password = password;
    const admin = $('server-admin-password-input').value;
    if (admin) body.admin_password = admin;

    const result = await post('/api/server/identity', body);
    if (result) {
      // Never leave a password sitting in the DOM.
      $('server-password-input').value = '';
      $('server-admin-password-input').value = '';
      if (result.reauth) setTimeout(() => location.reload(), 1200);
    }
  });
}

function saveVisibility() {
  post('/api/server/visibility', {
    lan_discoverable: $('server-lan-visibility').value === 'visible',
    internet_discoverable: $('server-internet-visibility').value === 'visible',
    broker: $('server-broker').value.trim(),
    stun_servers: $('server-stun').value
      .split(',')
      .map((entry) => entry.trim())
      .filter(Boolean),
  });
}

const profileSave = $('bt-profile-save');
if (profileSave) {
  profileSave.addEventListener('click', () => {
    post('/api/bluetooth/profile', { profile: $('bt-profile').value });
  });
}

const identitySave = $('bt-identity-save');
if (identitySave) {
  identitySave.addEventListener('click', () => {
    post('/api/bluetooth/identity', { identity: $('bt-identity').value });
  });
}

const visibilitySave = $('server-visibility-save');
if (visibilitySave) visibilitySave.addEventListener('click', saveVisibility);

// The dropdowns apply immediately; the broker address needs Save, because it is
// typed and half a hostname should not be submitted on every keystroke.
for (const id of ['server-lan-visibility', 'server-internet-visibility']) {
  const element = $(id);
  if (element) element.addEventListener('change', saveVisibility);
}

/* If a session cookie is still valid, skip the login screen. */
fetch('/api/status').then((response) => {
  if (response.ok) {
    $('login').classList.add('hidden');
    $('app').classList.remove('hidden');
    connect();
  }
}).catch(() => {});
