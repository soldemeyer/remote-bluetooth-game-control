/* Shared DOM helpers.
 *
 * The incremental-render discipline lives here: `setText`/`setHtml` write
 * only on change, `busy` refuses to touch a control the operator is using,
 * and `delegate` attaches one handler per container so it survives the
 * rebuilds that do happen. See app.js for why all three matter.
 */

'use strict';

export const $ = (id) => document.getElementById(id);


/* True while a mouse button or touch is held anywhere. A redraw during a click
 * is what ate the button presses, so we suspend updates for the duration. */
let pointerDown = false;

/* Read through a function, not exported directly: an imported binding is
 * a live *read-only* view, so a module that assigned to it would fail. */
export function isPointerDown() {
  return pointerDown;
}
addEventListener('pointerdown', () => { pointerDown = true; }, true);
addEventListener('pointerup', () => { pointerDown = false; }, true);
addEventListener('pointercancel', () => { pointerDown = false; }, true);

/** Should this control be left alone right now? */
export function busy(element) {
  return pointerDown || document.activeElement === element;
}

/* Does the focused element hold state we would destroy by rebuilding around it?
 *
 * A <select> being browsed and an <input> being typed into both hold something
 * the operator has not committed yet, so restructuring under them loses work.
 * A **button does not**: it is momentary, and by the time focus is on it the
 * click has already been dispatched.
 *
 * Telling them apart is load-bearing. A clicked button keeps focus afterwards,
 * so treating focus alone as "busy" meant the one action that changes a card's
 * structure -- approving a client -- left focus inside the very card that
 * needed rebuilding, and the rebuild was skipped for as long as the button
 * stayed focused. The click reached the server and the GUI never moved: the
 * pill still read PENDING and the Approve button stayed put, which is
 * indistinguishable from the button doing nothing at all. */
export function holdsUncommittedState(element) {
  if (!element) return false;
  const tag = element.tagName;
  return tag === 'SELECT' || tag === 'INPUT' || tag === 'TEXTAREA'
    || element.isContentEditable;
}

/** Write text only when it actually changed, to avoid pointless layout work. */
export function setText(element, text) {
  if (element && element.textContent !== text) element.textContent = text;
}

/** Write HTML only when it actually changed. */
export function setHtml(element, html) {
  if (element && element.innerHTML !== html) element.innerHTML = html;
}

/**
 * Fill a field from the server only when the server's own value has changed.
 *
 * The distinction that matters: "the field is empty" is not the same question
 * as "the server changed it". Writing on the first makes an empty field
 * impossible to keep -- status arrives at 10 Hz, so the field refills before
 * there is any window in which to save it. Writing on the second leaves the
 * operator's edits alone and still follows a change made elsewhere.
 *
 * Reported first for the video address, where whatever is in the field is
 * handed to every client, so a wrong value could not be removed by anyone.
 * The same guard was on the broker, the room code and the tunnel source.
 */
export function seedOnChange(field, value) {
  if (!field || busy(field)) return;
  const text = String(value);
  if (field.dataset.seeded === text) return;
  field.dataset.seeded = text;
  field.value = text;
}

export function stat(label, value) {
  return `<div class="stat">
            <div class="stat-label">${label}</div>
            <div class="stat-value">${value}</div>
          </div>`;
}

export function formatCount(value) {
  if (value === undefined || value === null) return '—';
  if (value < 1000) return String(value);
  if (value < 1e6) return `${(value / 1000).toFixed(1)}k`;
  return `${(value / 1e6).toFixed(2)}M`;
}

export function escapeHtml(value) {
  return String(value === undefined || value === null ? '' : value).replace(
    /[&<>"']/g,
    (ch) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[ch]),
  );
}

export async function withPending(element, run) {
  if (element.dataset.pending === '1') return;   // already in flight

  const wasDisabled = element.disabled;
  element.dataset.pending = '1';
  element.disabled = true;
  element.classList.add('pending');

  try {
    return await run();
  } finally {
    delete element.dataset.pending;
    element.disabled = wasDisabled;
    element.classList.remove('pending');
  }
}

export function delegate(containerId, handler) {
  const container = $(containerId);
  container.addEventListener('click', (event) => {
    const element = event.target.closest('[data-action]');
    if (element && container.contains(element) && element.tagName !== 'SELECT') {
      // Checkboxes and selects fire 'change' as well; only real buttons get
      // the pending treatment, since disabling a checkbox mid-toggle would
      // strand it showing the value the operator just moved away from.
      if (element.tagName === 'BUTTON') {
        withPending(element, () => handler(element));
      } else {
        handler(element);
      }
    }
  });
  container.addEventListener('change', (event) => {
    const element = event.target.closest('[data-action]');
    if (element && container.contains(element)) handler(element);
  });
}
