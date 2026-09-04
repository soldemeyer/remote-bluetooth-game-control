/* Part of the RBGC web GUI. See app.js for the whole picture. */

'use strict';

import { $, busy, holdsUncommittedState, isPointerDown, setHtml, setText, escapeHtml } from '../dom.js';
import { getLatest } from '../state.js';
import { updatePadPreview } from './pad.js';

/* ---------- clients ---------- */

export function renderClients(status) {
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
    // Focus on a *button* is not an interaction to protect -- see
    // holdsUncommittedState.
    const focused = document.activeElement;
    if (isPointerDown()
        || (focused && container.contains(focused) && holdsUncommittedState(focused))) {
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
      </tr>
      <tr class="preview-row" data-preview-row="${client.client_id}-${slot.slot}">
        <td colspan="6">
          <div class="pad-preview" data-field="preview"></div>
          <div class="muted small" data-field="preview-hint"></div>
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

    const preview = card.querySelector(
      `[data-preview-row="${client.client_id}-${slot.slot}"] [data-field="preview"]`);
    const hint = card.querySelector(
      `[data-preview-row="${client.client_id}-${slot.slot}"] [data-field="preview-hint"]`);
    if (preview) updatePadPreview(preview, hint, slot.input, slot.unbound);

    const select = row.querySelector('[data-action="assign"]');
    if (busy(select)) return;

    const channel = (status.adapters || []).find(
      (c) => c.assigned_client === client.client_id && c.assigned_slot === slot.slot);

    const available = (status.adapters || []).filter(
      (c) => !c.assigned_client ||
             (c.assigned_client === client.client_id && c.assigned_slot === slot.slot));

    // Label by what the operator calls the adapter ("Controller 2"), not by
    // hciX -- which reshuffles across reboots -- and not by the advertised
    // name, which is identical on every adapter whenever the identity is
    // impersonating a named product. Four indistinguishable options is not a
    // choice.
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

/** What the operator calls one adapter. Unique; the advertised name is not. */
export function adapterLabel(hw) {
  if (!hw) return '';
  return hw.display_name || (hw.number ? `Controller ${hw.number}` : hw.hci);
}

/** The same, looked up from a channel row. */
export function adapterName(channel) {
  if (!getLatest()) return channel.hci;
  const hardware = (getLatest().hardware || []).find((h) => h.bd_addr === channel.bd_addr);
  return adapterLabel(hardware) || channel.hci;
}

export function latencyCell(stats) {
  if (!stats || !stats.count) return '<span class="muted">—</span>';

  // Thresholds reflect what is achievable, not the impossible 2-5 ms target:
  // the Bluetooth hop alone costs 5-15 ms. See CLAUDE.md.
  const p50 = stats.p50;
  const cls = p50 < 25 ? 'latency-good' : p50 < 60 ? 'latency-ok' : 'latency-bad';
  return `<span class="${cls}">${p50.toFixed(1)} ms</span>` +
         `<span class="muted"> / p99 ${stats.p99.toFixed(1)}</span>`;
}
