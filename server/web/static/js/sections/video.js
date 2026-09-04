/* Part of the RBGC web GUI. See app.js for the whole picture. */

'use strict';

import { $, busy, setText, escapeHtml } from '../dom.js';
import { activeView } from '../nav.js';

/* ---------- video ----------
 *
 * Same discipline as everywhere else in this file: values are written in
 * place, never by rebuilding the section, and never into a control the
 * operator is currently touching. The device dropdowns get a signature so
 * their options are only rebuilt when the list genuinely changed -- otherwise
 * an open dropdown would close ten times a second.
 */

export function renderVideo(video) {
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

export function renderAudioMeter(status) {
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

export function renderVideoConnection(video) {
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

  const advertiseHost = $('video-advertise-host');
  if (advertiseHost && !busy(advertiseHost) && advertiseHost.value === '') {
    advertiseHost.value = connection.advertise_host || '';
  }

  const advertisePort = $('video-advertise-port');
  if (advertisePort && !busy(advertisePort) && advertisePort.value === '') {
    advertisePort.value = connection.advertise_port || '';
  }

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

export function renderDetectedServers(servers) {
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
export function applyDetectedSelection() {
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

export function renderVideoConfig(video) {
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

export function setPreviewRate(fps) {
  const wanted = Math.max(33, Math.round(1000 / Math.max(Number(fps) || 10, 1)));
  if (wanted === previewIntervalMs) return;
  previewIntervalMs = wanted;
  // Re-arm at the new rate if it is already running.
  if (previewTimer) {
    clearInterval(previewTimer);
    previewTimer = setInterval(fetchPreview, previewIntervalMs);
  }
}

export function startPreview() {
  if (previewTimer) return;
  // Never while its section is hidden. The preview is the one request in this
  // page that costs the *Bluetooth server* real work: slices are decoded and
  // reassembled on the datapath thread, which has a sub-millisecond budget.
  // Asking for frames nobody can see is that cost for nothing.
  if (activeView() !== 'video') return;
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

/* A predicate, not the handle. `previewTimer` is module state that
 * changes; an imported binding is a read-only snapshot, so a caller
 * testing it directly would see whatever it held at import time. */
export function previewRunning() {
  return previewTimer !== null;
}

export function stopPreview() {
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

/* Leaving the Video section stops the preview.
 *
 * Driven by an event so that `nav.js` need not import this module: it
 * already imports `activeView` from nav, and a cycle between the two
 * would leave one of them half-initialised at load. */
document.addEventListener('rbgc:viewchange', (event) => {
  if (event.detail && event.detail.view !== 'video') stopPreview();
});
