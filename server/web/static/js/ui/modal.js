/* Modal dialogs, on top of the platform's own.
 *
 * Native <dialog> + showModal(), not a hand-rolled overlay. That buys the focus
 * trap, the inertness of the page behind, Escape, and ::backdrop -- none of
 * which has to be written here or tested here. Hand-rolling a focus trap is the
 * shape of a bug this project has already paid for once: a filter installed on
 * the whole application and never removed left the client swallowing every key
 * press, with nothing on screen to say why.
 *
 * The markup is static in index.html rather than built here, so the tests can
 * assert on it and nothing is rebuilt by the 10 Hz status feed.
 *
 * **Fields are seeded once, at open.** `busy()` only protects the element that
 * currently has focus, and a dialog has several: an operator who types a server
 * name and then clicks the password field below it would have the first one
 * overwritten 100 ms later by the render loop. Seeding from `getLatest()` at
 * open time is the only arrangement where that cannot happen -- and it lets the
 * old `value === ''` guard go, which was the opposite of a guard: it refilled a
 * field the instant it was cleared.
 */

'use strict';

/**
 * Open a dialog, seed it, and put focus in its first control.
 *
 * `seed` is called *before* `showModal`, so the operator never sees the fields
 * change under them.
 */
export function openModal(id, seed) {
  const dialog = document.getElementById(id);
  if (!dialog || typeof dialog.showModal !== 'function') return null;
  if (dialog.open) return dialog;

  if (typeof seed === 'function') seed(dialog);

  // A tooltip lives at a z-index; a dialog lives in the top layer, which no
  // z-index reaches. One left open under the backdrop is unreadable and
  // cannot be dismissed.
  document.dispatchEvent(new CustomEvent('rbgc:modalopen', { detail: { id } }));

  dialog.showModal();

  const first = dialog.querySelector('input, select, textarea');
  if (first && typeof first.focus === 'function') first.focus();
  return dialog;
}

export function closeModal(id) {
  const dialog = document.getElementById(id);
  if (dialog && dialog.open && typeof dialog.close === 'function') dialog.close();
  return dialog;
}

/* Cancel buttons, declared rather than wired one at a time. `type="button"` on
   every one of them: a bare <button> inside a form submits it. */
document.addEventListener('click', (event) => {
  const close = event.target.closest ? event.target.closest('[data-close]') : null;
  if (!close) return;
  const dialog = close.closest('dialog');
  if (dialog && dialog.open) dialog.close();
});

/* Never leave a credential sitting in the DOM, however the dialog was closed --
   Escape and the backdrop both end up here, and neither runs a submit handler.
   The same rule the connect and identity forms already follow. */
document.addEventListener('close', (event) => {
  const dialog = event.target;
  if (!dialog || !dialog.querySelectorAll) return;
  dialog.querySelectorAll('input[type="password"]').forEach((field) => {
    field.value = '';
  });
}, true);
