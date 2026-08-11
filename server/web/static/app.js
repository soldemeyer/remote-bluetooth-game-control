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
  renderAdapters(status);
  renderClients(status);
  renderDatapath(status.datapath);
}

/* ---------- server control panel ---------- */

function renderServerPanel(status) {
  const server = status.server || {};

  const running = !!server.enabled;
  const toggle = $('server-enabled');
  if (toggle && !busy(toggle)) toggle.checked = running;

  const state = $('server-state');
  if (state) {
    setText(state, running ? 'Accepting clients' : 'Not accepting clients');
    state.className = running ? 'pill approved' : 'pill pending';
  }

  // Text inputs are only seeded when untouched, so typing is never clobbered.
  const name = $('server-name-input');
  if (name && !busy(name) && name.value === '') name.value = server.name || '';

  const visibility = $('server-visibility');
  if (visibility && !busy(visibility)) {
    visibility.value = server.discoverable ? 'broadcast' : 'hidden';
  }

  const internet = $('server-internet');
  if (internet && !busy(internet)) internet.checked = !!server.internet_enabled;

  const broker = $('server-broker');
  if (broker && !busy(broker) && broker.value === '') {
    broker.value = server.broker || '';
  }
}

/* ---------- adapters ---------- */

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
  return `
    <div class="card" data-card="${hw.bd_addr}">
      <div class="card-head">
        <div>
          <div class="card-title">
            <span class="status-dot" data-field="dot"></span><span data-field="hci"></span>
          </div>
          <div class="mono muted">${hw.bd_addr}</div>
          <div class="muted" data-field="manufacturer"></div>
        </div>
        <label class="toggle">
          <input type="checkbox" data-action="enable" data-addr="${hw.bd_addr}">
          <span>On</span>
        </label>
      </div>

      <div class="muted" data-field="state"></div>

      <div data-field="body" class="hidden">
        <div class="card-row">
          <label>Emulate</label>
          <select data-action="profile" data-addr="${hw.bd_addr}" style="flex:1"></select>
        </div>
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

  let dot = 'off';
  let stateText = 'Disabled';
  if (enabled && connected && ready) { dot = 'live'; stateText = 'Connected'; }
  else if (enabled && connected) { dot = 'waiting'; stateText = 'Connected, handshaking'; }
  else if (enabled) { dot = 'waiting'; stateText = 'Waiting for console'; }

  card.classList.toggle('disabled', !enabled);
  field('dot').className = `status-dot ${dot}`;
  setText(field('hci'), hw.hci);
  setText(field('manufacturer'), hw.manufacturer || '');
  setText(field('state'), stateText);

  const toggle = card.querySelector('[data-action="enable"]');
  if (!busy(toggle)) toggle.checked = enabled;

  field('body').classList.toggle('hidden', !enabled);
  if (!enabled) return;

  // The dropdown: options are rebuilt only if the profile list itself changed,
  // and never while the operator has it open. This is the bug that made the
  // menu close the moment it was opened.
  const select = card.querySelector('[data-action="profile"]');
  const profiles = status.profiles || [];
  const signature = profiles.map((p) => p.name).join(',');
  if (select.dataset.signature !== signature && !busy(select)) {
    select.innerHTML = profiles.map((p) =>
      `<option value="${p.name}">${escapeHtml(p.display_name)}</option>`).join('');
    select.dataset.signature = signature;
  }
  if (!busy(select) && channel.profile && select.value !== channel.profile) {
    select.value = channel.profile;
  }

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

    const signature = available.map((c) => `${c.bd_addr}:${c.profile_display}`).join(',');
    if (select.dataset.signature !== signature) {
      select.innerHTML = ['<option value="">— not assigned —</option>'].concat(
        available.map((c) =>
          `<option value="${c.bd_addr}">${escapeHtml(c.hci)} (${escapeHtml(c.profile_display)})</option>`),
      ).join('');
      select.dataset.signature = signature;
    }

    const wanted = channel ? channel.bd_addr : '';
    if (select.value !== wanted) select.value = wanted;
  });
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
  } else if (action === 'profile') {
    await post('/api/adapter/profile', { bd_addr, profile: element.value });
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

const serverEnabled = $('server-enabled');
if (serverEnabled) {
  serverEnabled.addEventListener('change', (event) => {
    post('/api/server/state', { enabled: event.target.checked });
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

const visibilityForm = $('server-visibility-form');
if (visibilityForm) {
  visibilityForm.addEventListener('submit', (event) => {
    event.preventDefault();
    post('/api/server/visibility', {
      discoverable: $('server-visibility').value === 'broadcast',
      internet_enabled: $('server-internet').checked,
      broker: $('server-broker').value.trim(),
    });
  });
}

/* If a session cookie is still valid, skip the login screen. */
fetch('/api/status').then((response) => {
  if (response.ok) {
    $('login').classList.add('hidden');
    $('app').classList.remove('hidden');
    connect();
  }
}).catch(() => {});
