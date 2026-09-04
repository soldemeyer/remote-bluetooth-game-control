/* Talking to the server, and the banner that reports how it went. */

'use strict';

import { $ } from './dom.js';

/* The banner's dismiss timer. Private to `showBanner` -- it was left behind in
   `dom.js` when this file was split out, and a module is always strict, so
   every call threw `ReferenceError` on the `clearTimeout` below. The throw
   lands *after* the text is written, so the banner still appeared: a saved
   setting reported "Could not reach the server" while the request had in fact
   succeeded. */
let bannerTimer = null;

/* ---------- API helpers ---------- */

export async function post(path, body) {
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

export function showBanner(text, kind, timeout = 5000) {
  const banner = $('banner');
  banner.textContent = text;
  banner.className = `banner ${kind || ''}`;
  banner.classList.remove('hidden');

  clearTimeout(bannerTimer);
  if (timeout > 0) {
    bannerTimer = setTimeout(() => banner.classList.add('hidden'), timeout);
  }
}
