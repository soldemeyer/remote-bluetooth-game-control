/* Part of the RBGC web GUI. See app.js for the whole picture. */

'use strict';

import { $, busy, setHtml, setText, escapeHtml } from '../dom.js';
import { adapterLabel } from './clients.js';

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
export function renderProfileChoice(status) {
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
export function renderIdentity(status) {
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

export function renderAdapters(status) {
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
  // The title is what the *operator* calls this adapter ("Controller 2"), not
  // what it advertises. Those are different strings on purpose: an identity
  // impersonating a named product sends the same name from every adapter, so
  // titling the cards with it made all four read "8BitDo 64 BT" and the
  // operator could not tell which card belonged to which player.
  //
  // The advertised name still matters -- it is what a console matches on -- so
  // it sits with hciX and the BD_ADDR underneath, where the diagnostics live.
  return `
    <div class="card" data-card="${hw.bd_addr}">
      <div class="card-head">
        <div>
          <div class="card-title">
            <span class="status-dot" data-field="dot"></span><span data-field="name"></span>
          </div>
          <div class="mono muted"><span data-field="hci"></span> &middot; ${hw.bd_addr}</div>
          <div class="muted">Advertises as <span data-field="advertised"></span></div>
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
        <!-- Two buttons, because a controller has two things you can do to
             it. The first swaps between Wake and Sleep with the state; the
             second is always Pair. "Forget pairing" is gone: pairing afresh
             is what it was for, and Pair now does it. -->
        <div class="card-row">
          <button class="small hidden" data-field="power-button"
                  data-action="wake" data-addr="${hw.bd_addr}">Wake</button>
          <button class="secondary small" data-field="pair-button"
                  data-action="pair" data-addr="${hw.bd_addr}">Pair</button>
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

  // An adapter the operator put to sleep is enabled, healthy and simply not
  // on the air. Reporting that as "Waiting for console" would be a lie in the
  // most expensive direction -- it is waiting for nothing, and nothing will
  // arrive until somebody wakes it.
  const stopped = enabled && hw.advertising === false;

  // unpaired | asleep | awake -- see AdapterState.power_state.
  const power = hw.power_state || 'unpaired';

  let dot = 'off';
  let stateText = 'Disabled';
  if (hidError) { dot = 'off'; stateText = 'Not serving HID'; }
  else if (enabled && connected && ready) { dot = 'live'; stateText = 'Awake — playing'; }
  else if (enabled && connected) { dot = 'waiting'; stateText = 'Awake — handshaking'; }
  // Unpaired is tested before "switched off", because a controller with no
  // console is not asleep *from* anything -- and after Reset all it is both,
  // where only one of the two tells the operator what to do next.
  else if (enabled && power === 'unpaired') {
    dot = stopped ? 'off' : 'waiting';
    stateText = stopped ? 'Not paired — press Pair' : 'Not paired — ready to pair';
  }
  else if (stopped) { dot = 'off'; stateText = 'Asleep — switched off'; }
  else if (enabled && power === 'awake') { dot = 'live'; stateText = 'Awake — connected'; }
  else if (enabled) { dot = 'waiting'; stateText = 'Asleep — waiting for its console'; }

  card.classList.toggle('disabled', !enabled);
  field('dot').className = `status-dot ${dot}`;
  setText(field('name'), adapterLabel(hw));
  setText(field('advertised'), hw.name || '—');
  setText(field('hci'), hw.hci);
  setText(field('manufacturer'), hw.manufacturer || '');
  setText(field('state'), stateText);

  // Pairing is a timed state with no feedback from the Bluetooth stack, so
  // say so explicitly and count down -- otherwise the operator presses
  // "Connection mode" and nothing visibly happens.
  // Hidden the moment a console is attached, whatever the deadline says.
  // The server clears the window on connect, so this is belt and braces --
  // but the countdown is exactly the sort of thing that survives one missed
  // update and then contradicts the state text right next to it.
  const pairing = field('pairing');
  const remaining = hw.pairing_s || 0;
  pairing.classList.toggle(
    'hidden', remaining <= 0 || !!hidError || power === 'awake');
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
         <button class="secondary small float-right"
                 data-action="unassign" data-addr="${hw.bd_addr}">Unassign</button>
       </div>`
    : '<div class="assigned-to muted">No controller assigned</div>');

  /* Two controls, matching what a controller actually offers.
   *
   * The old set -- Connection mode / Re-advertise / Stop / Disconnect /
   * Forget pairing -- described our implementation rather than the device,
   * and several combinations reached states with no way back. A pad is
   * unpaired, asleep, or awake, and from each of those there are at most two
   * useful things to do.
   *
   * Written in place -- label, action, class -- never rebuilt, because
   * replacing the node between mousedown and mouseup is what ate button
   * presses here before (see the header note). */
  const powerButton = field('power-button');
  const pairButton = field('pair-button');

  // Unpaired has no power button: there is no console to wake to, and sleeping
  // something that is not paired is just switching it off with no way to tell
  // that from broken.
  powerButton.classList.toggle('hidden', power === 'unpaired');

  if (!busy(powerButton) && power !== 'unpaired') {
    const awake = power === 'awake';
    powerButton.dataset.action = awake ? 'sleep' : 'wake';
    setText(powerButton, awake ? 'Sleep' : 'Wake');
    powerButton.classList.toggle('secondary', !awake);
    powerButton.title = awake
      ? 'Switch this controller off: drop the link and stop advertising, so '
        + 'the console cannot reconnect until you wake it. The pairing is kept.'
      : 'Switch this controller on. It advertises again and the console it is '
        + 'paired with should reconnect within a few seconds.';
  }

  if (!busy(pairButton)) {
    pairButton.title = power === 'unpaired'
      ? 'Advertise for a new console. Put the console into pairing mode first.'
      : 'Pair with a console again from scratch. This clears the existing '
        + 'pairing on our side, exactly as holding pair on a real controller '
        + 'does, so the console must be in pairing mode.';
  }

  setHtml(field('write-stats'), channel.write_ms && channel.write_ms.count
    ? `<div class="muted">BT write p50 ${channel.write_ms.p50} ms &middot;
        p99 ${channel.write_ms.p99} ms &middot; sent ${channel.reports_sent}</div>`
    : '');
}
