/* Part of the RBGC web GUI. See app.js for the whole picture. */

'use strict';

import { $, busy, setText } from '../dom.js';

/* ---------- server control panel ---------- */

export function renderServerPanel(status) {
  const server = status.server || {};

  const lan = $('server-lan-enabled');
  if (lan && !busy(lan)) lan.checked = !!server.lan_enabled;
  setToggleLabel(lan, server.lan_enabled);

  const internet = $('server-internet-enabled');
  if (internet && !busy(internet)) internet.checked = !!server.internet_enabled;
  setToggleLabel(internet, server.internet_enabled);

  const tunnel = $('server-tunnel-enabled');
  if (tunnel && !busy(tunnel)) tunnel.checked = !!server.tunnel_enabled;
  setToggleLabel(tunnel, server.tunnel_enabled);

  const state = $('server-state');
  if (state) {
    const paths = [];
    if (server.lan_enabled) paths.push('LAN');
    if (server.internet_enabled) paths.push('Internet');
    if (server.tunnel_enabled) paths.push('Tunnel');
    setText(state, paths.length ? `Accepting: ${paths.join(' + ')}` : 'Not accepting clients');
    state.className = paths.length ? 'pill approved' : 'pill pending';
  }

  // What the broker link is actually doing. This used to be one bit -- did
  // the client object exist -- which reads the same whether no broker was set,
  // one was set but never applied, or one is registered and working. The
  // reported fault was exactly that: a broker saved 22 hours after the process
  // started, so it was configured, inert, and indistinguishable from absent.
  const note = $('server-internet-note');
  if (note) setText(note, describeBroker(server.broker_status, server.broker));

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

  const room = $('server-room');
  if (room && !busy(room) && room.value === '') {
    room.value = server.room_code || '';
  }

  const tunnelSource = $('server-tunnel-source');
  if (tunnelSource && !busy(tunnelSource) && tunnelSource.value === '') {
    tunnelSource.value = server.tunnel_source || '';
  }

  // Say what the tunnel gate is actually admitting. "On" alone does not
  // distinguish a forwarder on this machine from one that accepts any direct
  // connection at all, and those are very different postures.
  const tunnelNote = $('server-tunnel-note');
  if (tunnelNote) {
    if (!server.tunnel_enabled) {
      setText(tunnelNote, 'Clients arriving via a forwarder — frp, a port forward, or a VPN.');
    } else if (server.tunnel_source) {
      setText(tunnelNote, `Accepting tunnelled clients from ${server.tunnel_source}.`);
    } else {
      setText(tunnelNote,
        'Accepting a tunnelled client from any address — set "Tunnel delivers from" ' +
        'under Visibility to narrow this to your forwarder.');
    }
  }
}

/* A toggle whose label always read "On" regardless of state -- so a switch
 * that was off still said On beside it. The label is the state, not a name. */
export function setToggleLabel(input, on) {
  if (!input) return;
  const label = input.parentElement && input.parentElement.querySelector('span');
  if (label) setText(label, on ? 'On' : 'Off');
}

export function describeBroker(status, configured) {
  const info = status || {};
  switch (info.state) {
    case 'registered': {
      const where = info.external ? `, seen at ${info.external}` : '';
      const punching = info.punching_at
        ? ` — punching toward ${info.punching_at} peer(s)`
        : '';
      return `Registered with ${info.broker} as room "${info.room}"${where}${punching}.`;
    }
    case 'connecting':
      return `Contacting ${info.broker || configured || 'the broker'} — no acknowledgement yet.`;
    case 'no_room':
      return 'A broker is set but no room code is. Set one below.';
    case 'internet_off':
      return 'A broker is set. Turn "Over the Internet" on to register with it.';
    case 'unreachable':
      return `Broker ${info.broker || configured || ''} could not be resolved. Check the address.`;
    default:
      return 'No rendezvous broker configured — set one below.';
  }
}
