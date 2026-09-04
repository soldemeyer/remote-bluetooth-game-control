/* Part of the RBGC web GUI. See app.js for the whole picture. */

'use strict';

import { $, setHtml, stat, formatCount } from '../dom.js';

/* ---------- datapath ---------- */

export function renderDatapath(stats) {
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



