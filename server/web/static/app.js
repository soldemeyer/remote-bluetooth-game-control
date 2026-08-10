/* RBGC server web GUI.
 *
 * Status arrives over a WebSocket at 10 Hz. Rendering is a full redraw of each
 * section, which is simple and fast enough at this data volume -- but form
 * controls the operator is actively using must not be clobbered mid-edit, so
 * open <select> elements are left alone during a redraw.
 */

'use strict';

const $ = (id) => document.getElementById(id);

let socket = null;
let latest = null;
let bannerTimer = null;

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
  $('server-name').textContent = status.server.name;
  $('server-sub').textContent =
    `UDP port ${status.server.client_port} · capacity ` +
    `${status.server.capacity} controller${status.server.capacity === 1 ? '' : 's'}`;

  const autoApprove = $('auto-approve');
  if (document.activeElement !== autoApprove) {
    autoApprove.checked = status.server.auto_approve;
  }

  renderAdapters(status);
  renderClients(status);
  renderDatapath(status.datapath);
}

function renderAdapters(status) {
  const container = $('adapters');
  const hardware = status.hardware || [];
  const channels = status.adapters || [];

  $('adapter-count').textContent =
    hardware.length ? `${channels.length} of ${hardware.length} enabled` : `${channels.length}`;

  // Mock mode reports no hardware, so fall back to rendering channels directly.
  const rows = hardware.length ? hardware : channels.map((c) => ({
    bd_addr: c.bd_addr, hci: c.hci, manufacturer: '', enabled: true, up: true,
  }));

  if (!rows.length) {
    container.innerHTML =
      '<div class="empty">No Bluetooth adapters detected. Plug in a dongle and click Rescan.</div>';
    return;
  }

  // Preserve any dropdown the operator currently has open.
  const focusedId = document.activeElement && document.activeElement.dataset
    ? document.activeElement.dataset.key : null;

  container.innerHTML = rows.map((hw) => {
    const channel = channels.find((c) => c.bd_addr === hw.bd_addr);
    return adapterCard(hw, channel, status);
  }).join('');

  if (focusedId) {
    const restored = container.querySelector(`[data-key="${focusedId}"]`);
    if (restored) restored.focus();
  }

  container.querySelectorAll('[data-action]').forEach((element) => {
    element.addEventListener(
      element.tagName === 'SELECT' ? 'change' : 'click',
      onAdapterAction,
    );
  });
}

function adapterCard(hw, channel, status) {
  const enabled = !!channel;
  const connected = channel && channel.connected;
  const ready = channel && channel.ready;

  let dot = 'off';
  let stateText = 'Disabled';
  if (enabled && connected && ready) { dot = 'live'; stateText = 'Connected'; }
  else if (enabled && connected) { dot = 'waiting'; stateText = 'Connected, handshaking'; }
  else if (enabled) { dot = 'waiting'; stateText = 'Waiting for console'; }

  const profileOptions = (status.profiles || []).map((p) =>
    `<option value="${p.name}"${channel && channel.profile === p.name ? ' selected' : ''}>` +
    `${escapeHtml(p.display_name)}</option>`).join('');

  const assignment = channel && channel.assigned_client
    ? `<div class="assigned-to">
         <strong>${escapeHtml(channel.username || 'unnamed')}</strong>
         &middot; slot ${channel.assigned_slot}
         <button class="secondary small" style="float:right"
                 data-action="unassign" data-addr="${hw.bd_addr}">Unassign</button>
       </div>`
    : '<div class="assigned-to muted">No controller assigned</div>';

  const writeStats = channel && channel.write_ms && channel.write_ms.count
    ? `<div class="muted">BT write p50 ${channel.write_ms.p50} ms &middot;
        p99 ${channel.write_ms.p99} ms &middot; sent ${channel.reports_sent}</div>`
    : '';

  return `
    <div class="card ${enabled ? '' : 'disabled'}">
      <div class="card-head">
        <div>
          <div class="card-title"><span class="status-dot ${dot}"></span>${escapeHtml(hw.hci)}</div>
          <div class="mono muted">${hw.bd_addr}</div>
          ${hw.manufacturer ? `<div class="muted">${escapeHtml(hw.manufacturer)}</div>` : ''}
        </div>
        <label class="toggle">
          <input type="checkbox" data-action="enable" data-addr="${hw.bd_addr}"
                 ${enabled ? 'checked' : ''}>
          <span>On</span>
        </label>
      </div>

      <div class="muted">${stateText}</div>

      ${enabled ? `
        <div class="card-row">
          <label>Emulate</label>
          <select data-action="profile" data-addr="${hw.bd_addr}"
                  data-key="profile-${hw.bd_addr}" style="flex:1">
            ${profileOptions}
          </select>
        </div>
        ${assignment}
        ${writeStats}
        <div class="card-row">
          <button class="small" data-action="pair" data-addr="${hw.bd_addr}">
            Connection mode
          </button>
          <button class="secondary small" data-action="unpair" data-addr="${hw.bd_addr}">
            Stop
          </button>
        </div>
      ` : ''}
    </div>`;
}

async function onAdapterAction(event) {
  const element = event.currentTarget;
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
}

function renderClients(status) {
  const container = $('clients');
  const clients = status.clients || [];
  $('client-count').textContent = `${clients.length} of ${status.server.max_clients}`;

  if (!clients.length) {
    container.innerHTML =
      '<div class="empty">No clients connected. Point a client at this server\'s address and port.</div>';
    return;
  }

  container.innerHTML = clients.map((client) => clientCard(client, status)).join('');

  container.querySelectorAll('[data-action]').forEach((element) => {
    element.addEventListener(
      element.tagName === 'SELECT' ? 'change' : 'click',
      onClientAction,
    );
  });
}

function clientCard(client, status) {
  const pending = client.state === 'PENDING';

  const actions = pending
    ? `<button class="good small" data-action="approve" data-client="${client.client_id}">Approve</button>
       <button class="danger small" data-action="deny" data-client="${client.client_id}">Deny</button>`
    : `<button class="danger small" data-action="deny" data-client="${client.client_id}">Disconnect</button>`;

  const rows = (client.slots || []).map((slot) => {
    const channel = (status.adapters || []).find(
      (c) => c.assigned_client === client.client_id && c.assigned_slot === slot.slot);

    const options = ['<option value="">— not assigned —</option>'].concat(
      (status.adapters || [])
        .filter((c) => !c.assigned_client ||
                       (c.assigned_client === client.client_id && c.assigned_slot === slot.slot))
        .map((c) => `<option value="${c.bd_addr}"${channel && channel.bd_addr === c.bd_addr ? ' selected' : ''}>`
                  + `${escapeHtml(c.hci)} (${escapeHtml(c.profile_display)})</option>`),
    ).join('');

    return `
      <tr>
        <td>${slot.slot}</td>
        <td>${escapeHtml(slot.username || '—')}</td>
        <td class="muted">${escapeHtml(slot.device_name || '—')}</td>
        <td>${slot.connected ? '' : '<span class="latency-bad">disconnected</span>'}</td>
        <td>${latencyCell(slot.rtt_ms)}</td>
        <td>
          <select data-action="assign" data-client="${client.client_id}"
                  data-slot="${slot.slot}" data-key="assign-${client.client_id}-${slot.slot}"
                  ${pending ? 'disabled' : ''}>
            ${options}
          </select>
        </td>
      </tr>`;
  }).join('');

  return `
    <div class="client ${pending ? 'pending' : ''}">
      <div class="client-head">
        <div>
          <strong>${escapeHtml(client.client_name || client.client_id.slice(0, 8))}</strong>
          <span class="pill ${pending ? 'pending' : 'approved'}">${client.state}</span>
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

function latencyCell(stats) {
  if (!stats || !stats.count) return '<span class="muted">—</span>';

  // Thresholds reflect what is achievable, not the impossible 2-5 ms target:
  // the Bluetooth hop alone costs 5-15 ms. See CLAUDE.md.
  const p50 = stats.p50;
  const cls = p50 < 25 ? 'latency-good' : p50 < 60 ? 'latency-ok' : 'latency-bad';
  return `<span class="${cls}">${p50.toFixed(1)} ms</span>` +
         `<span class="muted"> / p99 ${stats.p99.toFixed(1)}</span>`;
}

async function onClientAction(event) {
  const element = event.currentTarget;
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
}

function renderDatapath(stats) {
  if (!stats) return;

  const process = stats.process_ms || {};
  $('datapath').innerHTML = `
    ${stat('Packets received', formatCount(stats.packets_received))}
    ${stat('Dropped', formatCount(stats.packets_dropped))}
    ${stat('Unroutable', formatCount(stats.packets_unroutable))}
    ${stat('Decrypt failures', formatCount(stats.decrypt_failures))}
    ${stat('Server process p50', process.count ? `${process.p50}<small> ms</small>` : '—')}
    ${stat('Server process p99', process.count ? `${process.p99}<small> ms</small>` : '—')}
  `;
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

/* ---------- header actions ---------- */

$('auto-approve').addEventListener('change', (event) => {
  post('/api/settings', { auto_approve: event.target.checked });
});

$('rescan').addEventListener('click', () => post('/api/rescan'));

/* If a session cookie is still valid, skip the login screen. */
fetch('/api/status').then((response) => {
  if (response.ok) {
    $('login').classList.add('hidden');
    $('app').classList.remove('hidden');
    connect();
  }
}).catch(() => {});
