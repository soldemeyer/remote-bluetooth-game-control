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
 *
 * This file is the entry point: it owns the socket, dispatches one status
 * to the sections, and registers the delegated handlers. Everything it
 * calls lives under js/ -- see js/dom.js for the render discipline the
 * comment above describes.
 */

'use strict';

import { $, busy, setText, withPending, delegate } from './js/dom.js';
import { post, showBanner } from './js/api.js';
import { getLatest, setLatest } from './js/state.js';
import { showView, applyTheme, closeThemeMenu } from './js/nav.js';
import { renderOverview } from './js/sections/overview.js';
import { renderServerPanel } from './js/sections/server.js';
import { renderAdapters, renderIdentity } from './js/sections/adapters.js';
import { renderClients } from './js/sections/clients.js';
import { renderDatapath } from './js/sections/datapath.js';
import {
  applyDetectedSelection,
  previewRunning,
  renderDetectedServers,
  renderVideo,
  startPreview,
  stopPreview,
} from './js/sections/video.js';

let socket = null;

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
      setLatest(message.data);
      render(message.data);
    }
  };

  socket.onclose = () => {
    // Reconnect rather than silently going stale -- an operator staring at a
    // frozen page during a game would have no idea anything was wrong.
    showBanner('Connection to the server lost. Reconnecting...', 'error', 0);
    setTimeout(connect, 2000);
  };
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
  renderOverview(status);
}

/* ---------- delegated event handling ----------
 *
 * Bound once to the containers rather than to each control. Handlers therefore
 * survive the structural rebuilds above -- re-attaching per element on every
 * tick is what made clicks unreliable in the first place.
 */

/* Run a button's action with a visible pending state.
 *
 * Every one of these posts to the Pi and then waits on Bluetooth -- a
 * disconnect tears down an encrypted link, a re-advertise removes and re-adds a
 * kernel advertising instance. That is comfortably long enough for a button
 * that gives no feedback to read as one that did nothing, and the operator
 * reasonably clicks it again. A second disconnect mid-teardown, or a second
 * re-advertise, is not harmless.
 *
 * So: disable the control, swap in a spinner, and put it back when the request
 * settles. `finally`, because a failed request needs the button back just as
 * much as a successful one -- more, since that is when it will be retried.
 *
 * The button is left disabled for the caller's own duration only. The status
 * feed is the source of truth for what actually happened, and it arrives at
 * 10 Hz on its own.
 */


delegate('adapters', async (element) => {
  const action = element.dataset.action;
  const bd_addr = element.dataset.addr;

  if (action === 'enable') {
    await post('/api/adapter/enable', { bd_addr, enabled: element.checked });
  } else if (action === 'wake') {
    await post('/api/adapter/wake', { bd_addr });
  } else if (action === 'sleep') {
    await post('/api/adapter/disconnect', { bd_addr });
  } else if (action === 'pair') {
    /* Pair replaces the pairing, which is what holding pair on a real
     * controller does -- and it is the only way out of the state that has
     * cost this project the most time: our half of a bond surviving after the
     * console dropped its own, silently blocking every future attempt.
     *
     * Confirmed rather than instant, because it costs a working link if the
     * operator meant Wake. One dialog, not the two the old Forget button
     * needed: there is no longer an override to explain, since clearing our
     * half IS the action rather than a dangerous corner of it. */
    if (!confirm(
      'Pair this controller again? This clears the existing pairing on our '
      + 'side and starts fresh, so the console must be in pairing mode. If '
      + 'you only want it to reconnect to the console it already knows, use '
      + 'Wake instead.')) {
      return;
    }
    await post('/api/adapter/pair', { bd_addr, pairable: true, duration: 300 });
  } else if (action === 'unassign') {
    await post('/api/assign', { bd_addr });
  } else if (action === 'region-remove') {
    /* One region named, not the remaining set: the server computes against
     * what it has stored, so a removal cannot carry the browser's stale idea
     * of the other assignments back over somebody else's change. */
    await post('/api/adapter/regions', {
      bd_addr, remove: element.dataset.region,
    });
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
      const current = (getLatest().adapters || []).find(
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
      advertise_host: $('video-advertise-host').value.trim(),
      // Blank means "same as above", so send 0 rather than coercing to a port.
      advertise_port: Number($('video-advertise-port').value) || 0,
      password: $('video-password').value,
    });
    // Never leave a credential sitting in the form.
    $('video-password').value = '';
  } else if (action === 'video-probe') {
    await post('/api/video/probe', {});
  } else if (action === 'video-preview-toggle') {
    if (previewRunning()) stopPreview(); else startPreview();
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
    split_detect_enabled: $('video-split-detect').checked,
    split_crop_bars: $('video-split-crop-bars').checked,
    split_override: $('video-split-override').value,
  });
});

/* ---------- header + server panel actions ---------- */

$('auto-approve').addEventListener('change', (event) => {
  post('/api/settings', { auto_approve: event.target.checked });
});

$('rumble-enabled').addEventListener('change', (event) => {
  post('/api/settings', { rumble_enabled: event.target.checked });
});

$('rescan').addEventListener('click', () => post('/api/rescan'));

/* Reset all: the bulk form of Pair, and the useful unit of recovery when a
 * console has lost track of which controllers it knows.
 *
 * Confirmed, and the wording says what it costs rather than asking a vague
 * "are you sure": every controller has to be introduced to the console again,
 * and this console offers no way to forget one from its side. */
$('reset-all').addEventListener('click', (event) => withPending(
  event.currentTarget,
  async () => {
    if (!confirm(
      'Unpair every enabled controller and switch them off? Each one will '
      + 'then have to be paired with the console again, one at a time. '
      + 'Nothing else recovers a console that has lost track of which '
      + 'controllers it knows.')) {
      return;
    }
    await post('/api/adapter/reset-all');
  },
));

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

const tunnelEnabled = $('server-tunnel-enabled');
if (tunnelEnabled) {
  tunnelEnabled.addEventListener('change', (event) => {
    post('/api/server/state', { tunnel: event.target.checked });
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
    room_code: $('server-room').value.trim(),
    tunnel_source: $('server-tunnel-source').value.trim(),
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

/* ---------- split-screen regions: drag, or tap twice ----------
 *
 * Drag and drop is the asked-for gesture and the obvious one with a palette on
 * screen. It is **not** the only way in, for two reasons that are not
 * negotiable rather than nice-to-have:
 *
 *   - HTML5 drag events do not fire from touch. On a tablet -- a perfectly
 *     ordinary way to drive a headless Pi -- a drag-only control is a control
 *     that does nothing, with nothing on screen to say why.
 *   - A drag cannot be performed from the keyboard at all.
 *
 * So a region can also be *armed* by clicking it, and then placed by clicking
 * a controller. Both paths end in the same `place()`, so there is one thing to
 * get right. Arming is visible (the region lights up, and a line under the
 * palette says what will happen next) because a mode the operator cannot see
 * is worse than no mode.
 */

let armedRegion = null;

function setArmed(region) {
  armedRegion = region;
  document.querySelectorAll('#region-palette .region').forEach((el) => {
    el.classList.toggle('armed', el.dataset.region === region);
    el.setAttribute('aria-pressed', String(el.dataset.region === region));
  });
  /* One class on <body> rather than a class on each zone. Cards are rebuilt
   * whenever the *set* of adapters changes, so a zone marked individually
   * loses the mark if a dongle is plugged in mid-gesture -- and there is no
   * hook here to re-mark it, since the card renderer cannot see this module's
   * state. A body class needs no coordination at all: a card built a moment
   * later inherits it from CSS. */
  document.body.classList.toggle('region-armed', region !== null);
  setText($('region-armed-hint'), region
    ? 'Now click the controller that should show it. Esc to cancel.'
    : '');
}

async function place(bdAddr, region) {
  if (!bdAddr || !region) return;
  setArmed(null);
  /* `add`, not the whole set. The server replaces whatever held that region's
   * layout, computing against what it has stored -- so dropping a quadrant
   * onto a controller that already shows one swaps it, and two operators
   * cannot make one drop discard the other's. */
  await post('/api/adapter/regions', { bd_addr: bdAddr, add: region });
}

const palette = $('region-palette');
if (palette) {
  palette.addEventListener('dragstart', (event) => {
    const region = event.target.closest('.region');
    if (!region) return;
    event.dataTransfer.setData('text/plain', region.dataset.region);
    event.dataTransfer.effectAllowed = 'copy';
    // Armed as well, so a drag that is abandoned leaves the same visible
    // state a click would -- and dropping on a controller works either way.
    setArmed(region.dataset.region);
  });

  palette.addEventListener('click', (event) => {
    const region = event.target.closest('.region');
    if (!region) return;
    // A second click on the armed region disarms it, so the mode is
    // escapable without knowing about Esc.
    setArmed(armedRegion === region.dataset.region ? null : region.dataset.region);
  });
}

document.addEventListener('keydown', (event) => {
  if (event.key === 'Escape' && armedRegion) setArmed(null);
});

/* Delegated on the adapters container, because cards are rebuilt whenever the
 * *set* of adapters changes and a handler bound to a card would go with it. */
const adaptersContainer = $('adapters');
if (adaptersContainer) {
  adaptersContainer.addEventListener('dragover', (event) => {
    const zone = event.target.closest('.region-drop');
    if (!zone) return;
    // Without preventDefault the browser refuses the drop, silently.
    event.preventDefault();
    event.dataTransfer.dropEffect = 'copy';
    zone.classList.add('over');
  });

  adaptersContainer.addEventListener('dragleave', (event) => {
    const zone = event.target.closest('.region-drop');
    // `relatedTarget` is where the pointer went. Moving between the zone's own
    // children fires dragleave too, and clearing the highlight then makes it
    // flicker for the whole drag.
    if (zone && !zone.contains(event.relatedTarget)) zone.classList.remove('over');
  });

  adaptersContainer.addEventListener('drop', async (event) => {
    const zone = event.target.closest('.region-drop');
    if (!zone) return;
    event.preventDefault();
    zone.classList.remove('over');
    const region = event.dataTransfer.getData('text/plain') || armedRegion;
    await place(zone.dataset.drop, region);
  });

  adaptersContainer.addEventListener('click', (event) => {
    if (!armedRegion) return;
    // Not while dismissing a chip: the X sits inside the zone, and a click
    // that removes a region should not also place the armed one.
    if (event.target.closest('[data-action]')) return;
    const zone = event.target.closest('.region-drop');
    if (zone) place(zone.dataset.drop, armedRegion);
  });
}

