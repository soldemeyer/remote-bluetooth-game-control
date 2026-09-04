/* Part of the RBGC web GUI. See app.js for the whole picture. */

'use strict';

import { $, setText } from '../dom.js';

/* ---------- overview ----------
 *
 * A summary of the other five sections, so the landing view answers "is
 * everything working?" without a tour. Written in place like everything else
 * here: five values and five details, never a rebuilt container.
 *
 * Each card states its condition in words as well as colour. A red number
 * beside the word "streaming" would be worse than no colour at all.
 */

function setSummary(key, value, detail, state) {
  setText($(`ov-${key}-value`), value);
  setText($(`ov-${key}-detail`), detail);
  const card = document.querySelector(`.summary[data-view="${key}"]`);
  // Namespaced: bare `good`/`bad` collide with the `button.good` variant, and
  // these cards are buttons.
  if (card) card.className = `summary summary-${state}`;
}

export function renderOverview(status) {
  const server = status.server || {};
  const ways = [
    server.lan_enabled && 'this network',
    server.internet_enabled && 'the Internet',
    server.tunnel_enabled && 'a tunnel',
  ].filter(Boolean);
  setSummary(
    'server',
    ways.length ? 'Accepting' : 'Closed',
    ways.length ? `via ${ways.join(', ')}` : 'nobody can connect',
    ways.length ? 'good' : 'idle',
  );

  // `status.adapters` is the router's *channels* -- the ones already enabled --
  // and carries neither `enabled` nor `phase`. Filtering it on `a.enabled`
  // therefore matched nothing, so this card read "0/0 - none enabled" on a
  // server with four adapters linked to a console, and could never have read
  // anything else. The adapter state lives on `status.hardware`, which is the
  // list the Bluetooth section counts; both must come from the same place or
  // the two views disagree about the same hardware.
  //
  // Mock mode reports no hardware, so fall back to the channels -- the same
  // fallback `renderAdapters` makes, for the same reason.
  const channels = status.adapters || [];
  const hardware = status.hardware || [];
  const enabled = hardware.length ? hardware.filter((a) => a.enabled) : channels;
  const linked = hardware.length
    ? enabled.filter((a) => a.phase === 'linked')
    : channels.filter((c) => c.connected);
  const degraded = hardware.length
    ? enabled.filter((a) => a.phase === 'degraded')
    : [];
  setSummary(
    'adapters',
    `${linked.length}/${enabled.length || 0}`,
    degraded.length
      ? `${degraded.length} degraded`
      : enabled.length
        ? 'linked to a console'
        : 'none enabled',
    degraded.length ? 'bad' : (enabled.length && linked.length ? 'good' : 'idle'),
  );

  const clients = status.clients || [];
  const pending = clients.filter((c) => !c.approved);
  setSummary(
    'clients',
    String(clients.length),
    pending.length ? `${pending.length} waiting for approval` : 'connected',
    pending.length ? 'warn' : (clients.length ? 'good' : 'idle'),
  );

  const video = status.video || {};
  const streaming = Boolean(video.streaming || (video.source && video.source.available));
  setSummary(
    'video',
    streaming ? 'Live' : (video.mode && video.mode !== 'off' ? 'Waiting' : 'Off'),
    streaming ? `${video.clients || 0} watching` : (video.error || 'no source'),
    streaming ? 'good' : (video.error ? 'bad' : 'idle'),
  );

  const datapath = status.datapath || {};
  const received = datapath.packets_received || 0;
  const dropped = datapath.dropped || 0;
  const unroutable = datapath.unroutable || 0;
  // Green only once something has actually arrived. "None lost" of nothing is
  // not health, and a green card over a datapath nobody is using is the same
  // confidently-wrong display this page keeps having to unpick.
  setSummary(
    'datapath',
    String(received),
    dropped || unroutable
      ? `${dropped} dropped, ${unroutable} unroutable`
      : received
        ? 'packets received, none lost'
        : 'no packets yet',
    dropped || unroutable ? 'warn' : (received ? 'good' : 'idle'),
  );
}
