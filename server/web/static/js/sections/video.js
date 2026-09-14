/* Part of the RBGC web GUI. See app.js for the whole picture. */

'use strict';

import { $, busy, seedOnChange, setText, escapeHtml } from '../dom.js';
import { activeView } from '../nav.js';
import { setToggleLabel } from './server.js';

/* ---------- video ----------
 *
 * Same discipline as everywhere else in this file: values are written in
 * place, never by rebuilding the section, and never into a control the
 * operator is currently touching. The device dropdowns get a signature so
 * their options are only rebuilt when the list genuinely changed -- otherwise
 * an open dropdown would close ten times a second.
 */

/* Lives in dom.js now, beside the other render-discipline helpers, because
 * the Visibility fields had the identical bug. Re-exported so this module
 * stays the obvious place to look for it. */
export { seedOnChange };

export function renderVideo(video) {
  /* First, and outside the early return below: the split-screen controls live
     on the *Controllers* view, so they have to be dealt with even when this
     section is hidden. Left alone they would sit at their markup defaults --
     reading "off" for settings that are simply unknown, and posting to an
     endpoint that is not there. */
  renderSplitControls(video);

  const section = $('video-section');
  if (!video) {
    if (section) section.classList.add('hidden');
    return;
  }
  if (section) section.classList.remove('hidden');

  renderVideoVisibility(video);

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
  setText($('video-broker'), describeVideoBroker(video.broker_status));
  setText($('video-layout'), describeLayout(video));

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

/* Which cards this mode actually has settings for.
 *
 * A card that cannot apply to the chosen source is not merely noise; it
 * invites the operator to configure something that will not take. The three
 * modes want three different pages:
 *
 *   off       nothing to say. No connection, no capture settings, no preview.
 *   external  the address and password to reach the source, and a preview of
 *             what it is sending. **Not its capture settings**: the video
 *             server owns those, and pushing ours over them is what reverted
 *             the operator's choices the moment this server connected.
 *   embedded  the capture settings and the preview. There is nothing to point
 *             at or authenticate to -- the source is this machine's own
 *             subprocess and the password is generated for it.
 */
export function renderVideoVisibility(video) {
  const mode = video.mode;

  const connection = $('video-connection');
  if (connection) connection.classList.toggle('hidden', mode !== 'external');

  const preview = $('video-preview-card');
  if (preview) preview.classList.toggle('hidden', mode === 'off');

  const config = $('video-config-card');
  if (config) config.classList.toggle('hidden', mode !== 'embedded');

  /* **Hiding the card is not enough.** `startPreview` refuses while another
     view is showing, but knows nothing about the mode -- so without this a
     10 Hz poll carries on against a card nobody can see, and that request is
     the one thing on this page that costs the *datapath thread* real work:
     slices are decoded and reassembled on a thread with a sub-millisecond
     budget. */
  if (mode === 'off' && previewRunning()) stopPreview();
}

/* The split-screen detector's settings, which live on the Controllers view
 * beside the region assignments they govern -- that is the question they
 * answer, rather than "how is this captured".
 *
 * They are video-source settings, so a server built without video has none:
 * say so rather than showing three controls that cannot take. */
export function renderSplitControls(video) {
  const panel = $('split-settings');
  if (!panel) return;

  const settings = video && video.settings;
  const available = Boolean(video);
  panel.classList.toggle('unavailable', !available);
  setText($('split-unavailable-hint'), available
    ? ''
    : 'This server was built without video, so there is nothing to detect '
      + 'a split in. Assignments above are kept and take effect if a video '
      + 'source is added.');

  for (const id of ['video-split-detect', 'video-split-crop-bars', 'video-split-override']) {
    const element = $(id);
    if (element) element.disabled = !available;
  }
  if (!settings) return;

  const detect = $('video-split-detect');
  if (detect && !busy(detect)) detect.checked = !!settings.split_detect_enabled;
  setToggleLabel(detect, settings.split_detect_enabled);

  const crop = $('video-split-crop-bars');
  if (crop && !busy(crop)) crop.checked = !!settings.split_crop_bars;
  setToggleLabel(crop, settings.split_crop_bars);

  const override = $('video-split-override');
  if (override && !busy(override)) override.value = settings.split_override || 'auto';
}

/* The address, port and password we use to reach the video server. Whether the
 * panel is shown at all is decided by `renderVideoVisibility`, so there is one
 * owner for that rather than two rules that can disagree. */

export function renderVideoConnection(video) {
  const panel = $('video-connection');
  if (!panel) return;

  const connection = video.connection || {};

  const host = $('video-host');
  if (host && !busy(host)) host.value = connection.host || '';

  const port = $('video-port');
  if (port && !busy(port)) port.value = connection.port || 47810;

  // **These two cannot be emptied without this.** The guard used to be
  // `value === ''`, meant to avoid overwriting what the operator was typing --
  // but its actual effect was to refill the field the instant it was cleared,
  // and status arrives at 10 Hz, so there was no window in which to press
  // Save. Reported as "I'm unable to delete the value".
  //
  // It matters more than a stuck field: whatever is in here is handed to every
  // client as the address to fetch video from, so a wrong value that cannot be
  // removed breaks video for everyone off the LAN with no way back.
  //
  // Seeding on *change* instead leaves a cleared field cleared, still follows
  // the server when something else moves it, and still never writes to a
  // control while it is being used.
  seedOnChange($('video-advertise-host'), connection.advertise_host || '');
  seedOnChange($('video-advertise-port'), connection.advertise_port || '');

  /* **Null is a state, not a missing value.** `connection.link` is null when
     there is no link object at all, which since Disconnect exists is a place
     the operator can deliberately put the server. Folding it into `{}` made it
     indistinguishable from a link that exists and has not connected yet, so
     pressing Disconnect left the line reading "Connecting…" over a server that
     was doing nothing of the kind. */
  const link = connection.link;
  const connected = Boolean(link && link.connected);

  const hint = $('video-password-hint');
  if (hint) {
    if (!connection.host) {
      setText(hint, 'Detect a video server, or type its address.');
    } else if (!connection.has_password) {
      setText(hint, 'Enter the password shown on the video server.');
    } else if (connected) {
      setText(hint, `Connected to ${connection.host}:${connection.port}.`);
    } else if (!link) {
      setText(hint,
        `Not connected to ${connection.host}:${connection.port}. `
        + 'Press Connect to use it.');
    } else {
      setText(hint, link.last_error || 'Connecting…');
    }
  }

  /* One button, two actions. There was no way to let go of a video server
     short of switching video off, which also stops it being advertised to
     clients and is a different intent.

     Rewritten in place -- label, action, class -- and never replaced: a node
     swapped between mousedown and mouseup eats the click, which is the failure
     this GUI already records for the adapter cards' Wake/Sleep button. Skipped
     while the operator is on it, like every other control here. */
  const button = $('video-connect');
  if (button && !busy(button)) {
    button.dataset.action = connected ? 'video-disconnect' : 'video-connect';
    setText(button, connected ? 'Disconnect' : 'Connect');
    button.classList.toggle('secondary', connected);
    button.title = connected
      ? 'Stop talking to this video server. The address and password are kept, '
        + 'so Connect brings it back without retyping them.'
      : 'Connect to the video server at the address above.';
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

  /* Locked while a detected server is selected, so there is never a question
     of which of the two is being used. That lock *is* the signal -- the two
     sentences that used to sit under these fields said the same thing in
     prose, under controls that had already shown it. */
  host.disabled = !!chosen;
  port.disabled = !!chosen;
}

/* What the detector currently believes, in the operator's words.
 *
 * Three states that must be told apart, because they look identical from a
 * player's seat and want completely different actions: detection is off,
 * detection is on and says the picture is whole, and a layout is being
 * forced. The confidence is shown for the middle one only -- it means
 * nothing for an override, and quoting a number there would invite somebody
 * to tune against it. */
const LAYOUT_NAMES = {
  FULL: 'Full screen',
  VERTICAL_2: 'Two, side by side',
  HORIZONTAL_2: 'Two, stacked',
  QUAD_4: 'Four',
};

export function describeLayout(video) {
  const status = video.status || {};
  const block = status.layout;
  if (!block || !block.mode) return '—';

  const name = LAYOUT_NAMES[block.mode] || block.mode;
  if (block.source === 'override') return `${name} — forced`;

  const settings = video.settings || {};
  if (!settings.split_detect_enabled) return `${name} — detection off`;

  const confidence = Math.round((block.confidence || 0) * 100);
  return `${name} — detected, ${confidence}% confidence`;
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
  setToggleLabel(audio, settings.audio_enabled);

  const test = $('video-test-source');
  if (test && !busy(test)) test.checked = !!settings.test_source;
  setToggleLabel(test, settings.test_source);
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

/**
 * The video leg of the rendezvous room, in words.
 *
 * Separate from the gameplay leg's, and it exists because the two are
 * separate pairs with separate copies of the same settings. When they drifted,
 * a remote player got working controller input and a picture stuck on
 * "Connecting" -- and nothing on this page distinguished that from an operator
 * who had simply not asked for video over the Internet.
 *
 * `acknowledged` deliberately does not say "registered": that happens on the
 * source and it does not report back, so the honest claim is that the source
 * has the configuration carrying the broker.
 */
export function describeVideoBroker(status) {
  const info = status || {};
  switch (info.state) {
    case 'acknowledged':
      return `Source has room "${info.room}" on ${info.broker}.`;
    case 'pending':
      return `Telling the source about ${info.broker} — not acknowledged yet.`;
    case 'no_source':
      return `Room "${info.room}" is set, but no video source is connected.`;
    case 'not_applied':
      return `Broker ${info.broker || ''} is set for players but has not reached ` +
             'video. Save Visibility again to apply it.';
    case 'no_room':
      return 'A broker is set but no room code is.';
    case 'internet_off':
      return 'A broker is set. Turn "Over the Internet" on to use it for video.';
    default:
      return 'Not configured — players can only watch over the local network.';
  }
}
