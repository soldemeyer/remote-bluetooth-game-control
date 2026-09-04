/* Part of the RBGC web GUI. See app.js for the whole picture. */

'use strict';

import { $ } from '../dom.js';

/* ---------------------------------------------------------------------------
 * Live controller preview
 *
 * This exists because no counter can answer the question it answers. A client
 * can be connected, approved, assigned, streaming thousands of packets with
 * zero drops, and still be sending nothing but a neutral controller -- and
 * every indicator in this GUI stays green while it happens. That is not
 * hypothetical: a console ignored every input for an evening, and the fault
 * turned out to be that 1378 byte-identical idle HID reports had gone out over
 * Bluetooth. The presses never reached the server at all.
 *
 * So this draws what the *server* received, not what the client believes it
 * sent. Seeing a button light here proves the whole chain up to this point is
 * working and moves the search downstream; seeing nothing light proves the
 * opposite just as firmly. Either way it replaces an evening of guessing.
 *
 * The art is the same generated SVG the client GUI uses, so the two cannot
 * drift. Controls are groups keyed `c_<name>`; we only toggle a class on them,
 * which means improving the artwork never touches this code.
 * ------------------------------------------------------------------------ */

/* Logical button bits, matching common/state.py:Button. The server sends the
 * raw mask, so this table is the one place the two representations meet. */
const BUTTON_BITS = {
  c_a: 1 << 0,
  c_b: 1 << 1,
  c_x: 1 << 2,
  c_y: 1 << 3,
  c_lb: 1 << 4,
  c_rb: 1 << 5,
  c_back: 1 << 6,
  c_start: 1 << 7,
  c_guide: 1 << 8,
  c_lstick: 1 << 9,
  c_rstick: 1 << 10,
  c_dup: 1 << 11,
  c_ddown: 1 << 12,
  c_dleft: 1 << 13,
  c_dright: 1 << 14,
  c_lt: 1 << 16,
  c_rt: 1 << 17,
};

/* Fetched once for the page, not once per slot. */
let padArtPromise = null;

function padArt() {
  if (padArtPromise === null) {
    padArtPromise = fetch('/controllers/logical.svg', { credentials: 'same-origin' })
      .then((r) => (r.ok ? r.text() : null))
      .catch(() => null);
  }
  return padArtPromise;
}

/* How far a stick is drawn from centre, in SVG units. Small on purpose: this
 * is a "did it move, and which way" indicator, not a calibration tool. */
const STICK_TRAVEL = 12;

export function updatePadPreview(host, hint, input, unbound) {
  if (!input) {
    /* An older server, or a slot that has not reported yet. Say so rather
     * than drawing a controller that will never light up, which reads as
     * "your presses are being lost". */
    if (hint && !hint.textContent) hint.textContent = 'No input reported yet.';
    return;
  }

  if (!host.dataset.loaded) {
    if (host.dataset.loading) return;
    host.dataset.loading = '1';
    padArt().then((svg) => {
      if (!svg) {
        host.dataset.loading = '';
        if (hint) hint.textContent = 'Controller artwork could not be loaded.';
        return;
      }
      host.innerHTML = svg;
      host.dataset.loaded = '1';
      host.dataset.loading = '';
    });
    return;
  }

  const pressed = (id) => (input.buttons & BUTTON_BITS[id]) !== 0;

  Object.keys(BUTTON_BITS).forEach((id) => {
    /* Scoped to this host, so several slots can each hold a copy of the same
     * artwork without their duplicate ids colliding. */
    const el = host.querySelector(`[id="${id}"]`);
    if (el) el.classList.toggle('pressed', pressed(id));
  });

  /* Triggers are analog, so a partial pull should show as partial. The bit is
   * derived from the axis by the client, so a pad with digital triggers still
   * lights the control -- see apply_trigger_buttons. */
  const trigger = (id, value) => {
    const el = host.querySelector(`[id="${id}"]`);
    if (!el) return;
    const pulled = value > 8 || pressed(id);
    el.classList.toggle('pressed', pulled);
  };
  trigger('c_lt', input.left_trigger || 0);
  trigger('c_rt', input.right_trigger || 0);

  const stick = (id, x, y) => {
    const el = host.querySelector(`[id="${id}"]`);
    if (!el) return;
    const dx = ((x || 0) / 32768) * STICK_TRAVEL;
    const dy = ((y || 0) / 32768) * STICK_TRAVEL;
    el.setAttribute('transform', `translate(${dx.toFixed(1)} ${dy.toFixed(1)})`);
  };
  stick('c_lstick', input.left_x, input.left_y);
  stick('c_rstick', input.right_x, input.right_y);

  if (hint) {
    const idle = input.buttons === 0
      && Math.abs(input.left_x || 0) < 3000 && Math.abs(input.left_y || 0) < 3000
      && Math.abs(input.right_x || 0) < 3000 && Math.abs(input.right_y || 0) < 3000
      && (input.left_trigger || 0) < 8 && (input.right_trigger || 0) < 8;
    /* An unbound pad is neutral for a reason the operator can act on, and
     * saying "no input" there would be true and useless -- it is exactly the
     * message that sends someone to debug the console. */
    if (unbound) {
      hint.textContent =
        'This controller has no bindings, so it can only ever send a neutral '
        + 'state. Choose a configuration for it in the client.';
      hint.className = 'latency-bad small';
    } else {
      hint.textContent = idle
        ? 'Neutral — the server is receiving packets but no button or stick input.'
        : 'Receiving input.';
      hint.className = 'muted small';
    }
  }
}
