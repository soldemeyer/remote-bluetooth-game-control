/* Part of the RBGC web GUI. See app.js for the whole picture. */

'use strict';

import { isPointerDown } from '../dom.js';
import { DEFAULT_LAYOUT, PAD_LAYOUTS } from './pad_layouts.js';

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
 * It lives on the **adapter card** now rather than in the client table. The
 * card is what an operator means by "player 2" -- it is where they pair it,
 * wake it and give it its half of the screen -- so it is where the question
 * "is this one actually sending anything" gets asked.
 *
 * The art is the same generated SVG the client GUI uses, drawn for the family
 * the player configured. Controls are groups keyed `c_<name>`; we only toggle
 * a class on them, which means improving the artwork never touches this code.
 * ------------------------------------------------------------------------ */

/* One fetch per family for the whole page, not one per card. */
const padArt = new Map();

function artFor(file) {
  if (!padArt.has(file)) {
    padArt.set(file, fetch(`/controllers/${file}`, { credentials: 'same-origin' })
      .then((r) => (r.ok ? r.text() : null))
      .catch(() => null));
  }
  return padArt.get(file);
}

/* How far a stick is drawn from centre, in SVG units. Small on purpose: this
 * is a "did it move, and which way" indicator, not a calibration tool. */
const STICK_TRAVEL = 12;

/**
 * Which family's art to draw for a reported layout.
 *
 * A client one version ahead of this server can name a family we have never
 * heard of. Falling back is the whole of the handling: an unknown name must
 * not blank the card, because a blank card reads as a controller that is not
 * there.
 */
export function resolveFamily(layout) {
  return Object.prototype.hasOwnProperty.call(PAD_LAYOUTS, layout || '')
    ? layout
    : DEFAULT_LAYOUT;
}

/** The placeholder for an adapter with nothing assigned to it. */
function showGhost(host, hint) {
  if (host.dataset.family !== 'ghost') {
    if (isPointerDown()) return;
    host.dataset.family = 'ghost';
    host.dataset.sig = '';
    host.classList.add('ghost');
    artFor('ghost.svg').then((svg) => {
      if (svg && host.dataset.family === 'ghost') host.innerHTML = svg;
    });
  }
  /* Deliberately silent. The assignment row directly above already says
     "No controller assigned", and saying it twice in two type sizes reads as
     two different problems rather than one empty slot. */
  if (hint) {
    hint.textContent = '';
    hint.className = 'muted small';
  }
}

export function updatePadPreview(host, hint, input, unbound, layout) {
  if (!host) return;

  if (!input) {
    /* Nothing assigned, or a slot that has not reported yet. A ghost rather
       than a working pad that will never light: a controller drawn as present
       and permanently neutral reads as "your presses are being lost". */
    showGhost(host, hint);
    return;
  }

  const family = resolveFamily(layout);
  const spec = PAD_LAYOUTS[family];

  if (host.dataset.family !== family) {
    /* Replacing these nodes mid-gesture is the pair of failures this file's
       neighbours already record: a drag dropped, and a click lost because
       mousedown and mouseup landed on different nodes. The card also holds
       the region drop zone and two buttons. */
    if (isPointerDown()) return;
    host.dataset.family = family;
    host.dataset.sig = '';
    host.classList.remove('ghost');
    host.innerHTML = '';
    artFor(spec.svg).then((svg) => {
      if (!svg) {
        if (hint) hint.textContent = 'Controller artwork could not be loaded.';
        return;
      }
      if (host.dataset.family !== family) return;   // changed while we waited
      host.innerHTML = svg;
      host.dataset.sig = '';                        // force the next repaint
    });
    return;
  }

  if (!host.firstChild) return;                     // art still in flight

  /* **Skip the whole repaint when nothing moved.** Four adapter cards at
     10 Hz is ~76 DOM writes a second for a player sitting still, all of them
     setting a class to the value it already has. */
  const signature = `${input.buttons}:${input.left_x}:${input.left_y}:`
    + `${input.right_x}:${input.right_y}:`
    + `${input.left_trigger}:${input.right_trigger}`;
  if (host.dataset.sig !== signature) {
    host.dataset.sig = signature;
    paint(host, spec, input);
  }

  if (hint) describe(hint, input, unbound);
}

function paint(host, spec, input) {
  /* Scoped to this host, so several cards can each hold a copy of the same
     artwork without their duplicate ids colliding. */
  const find = (id) => host.querySelector(`[id="${id}"]`);

  for (const [id, bit] of Object.entries(spec.buttons)) {
    const element = find(id);
    if (element) element.classList.toggle('pressed', (input.buttons & bit) !== 0);
  }

  /* Triggers are analog, so a partial pull should show as partial. The bit is
     derived from the axis by the client, so a pad with digital triggers still
     lights the control -- see apply_trigger_buttons. A family whose trigger is
     a plain switch (the N64's Z) has it in `buttons` instead, and is handled
     by the loop above. */
  const travel = { c_lt: input.left_trigger || 0, c_rt: input.right_trigger || 0 };
  for (const [id, bit] of Object.entries(spec.triggers)) {
    const element = find(id);
    if (!element) continue;
    const pulled = (travel[id] || 0) > 8 || (input.buttons & bit) !== 0;
    element.classList.toggle('pressed', pulled);
  }

  const axes = {
    c_lstick: [input.left_x, input.left_y],
    c_rstick: [input.right_x, input.right_y],
  };
  for (const id of spec.sticks) {
    const element = find(id);
    if (!element) continue;
    const [x, y] = axes[id] || [0, 0];
    const dx = ((x || 0) / 32768) * STICK_TRAVEL;
    const dy = ((y || 0) / 32768) * STICK_TRAVEL;
    element.setAttribute('transform', `translate(${dx.toFixed(1)} ${dy.toFixed(1)})`);
  }
}

function describe(hint, input, unbound) {
  const idle = input.buttons === 0
    && Math.abs(input.left_x || 0) < 3000 && Math.abs(input.left_y || 0) < 3000
    && Math.abs(input.right_x || 0) < 3000 && Math.abs(input.right_y || 0) < 3000
    && (input.left_trigger || 0) < 8 && (input.right_trigger || 0) < 8;

  /* An unbound pad is neutral for a reason the operator can act on, and
     saying "no input" there would be true and useless -- it is exactly the
     message that sends someone to debug the console. */
  const text = unbound
    ? 'This controller has no bindings, so it can only ever send a neutral '
      + 'state. Choose a configuration for it in the client.'
    : idle
      ? 'Neutral — packets are arriving with no button or stick input.'
      : 'Receiving input.';
  const className = unbound ? 'latency-bad small' : 'muted small';

  if (hint.textContent !== text) hint.textContent = text;
  if (hint.className !== className) hint.className = className;
}
