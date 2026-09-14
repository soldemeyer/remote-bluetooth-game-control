/* Info icons, and the one tooltip they all share.
 *
 * This page used to explain itself with a paragraph under every control. That
 * is honest, and it is also why the Server view was two screens tall: a card
 * stopped reading as a set of controls. The prose now hangs off an icon beside
 * the label it belongs to.
 *
 * **One tip element, at the end of <body>, not one per trigger.** Every `.card`
 * carries a `backdrop-filter`, which makes it a stacking context painted
 * atomically in document order -- so a popup rendered *inside* card N is
 * covered by card N+1, and raising the cards does not help, because equal
 * z-indexes fall back to document order. style.css records exactly this bug for
 * the theme menu. A body-level element sits in the root stacking context after
 * every card, so one z-index wins outright; it also escapes `overflow`
 * clipping, which the rail has under 640px and the video preview has always.
 *
 * Positioned through `el.style.left`, which is CSSOM rather than an inline
 * style attribute, so the Content-Security-Policy is satisfied -- the audio
 * meter already does this.
 *
 * Three ways in, and two of them are not optional here:
 *
 *   - hover, for a mouse;
 *   - click, because a tablet driving a headless Pi is an ordinary way to run
 *     this and touch has no hover at all;
 *   - focus, because a hover-free keyboard user otherwise gets nothing.
 *     `aria-describedby` is set while open so the text is announced rather
 *     than merely drawn.
 *
 * The text lives in exactly one place -- `data-info` on the trigger -- so there
 * is no second copy to drift.
 */

'use strict';

export const TIP_ID = 'info-tip';

//: Between the trigger and the tip.
const GAP = 8;
//: Minimum distance from a viewport edge.
const EDGE = 8;

//: The trigger whose text is showing, and what opened it. A tip opened by
//: hover closes when the pointer leaves; one opened by a click is sticky,
//: because on a touchscreen there is no "leave" to close it with.
let openTrigger = null;
let openedBy = null;

/**
 * Where a tip of `size` goes for a trigger at `rect`.
 *
 * The only decision in this module, and the only part worth testing without a
 * browser: prefer below, flip above only when there is no room below *and*
 * there is room above, and clamp horizontally so it can never leave the
 * viewport however near an edge the trigger sits.
 */
export function chooseTooltipPosition(rect, size, viewport) {
  const below = rect.bottom + GAP;
  const above = rect.top - GAP - size.height;
  const flipped =
    below + size.height > viewport.height - EDGE && above >= EDGE;

  const wanted = rect.left + rect.width / 2 - size.width / 2;
  const limit = Math.max(EDGE, viewport.width - size.width - EDGE);
  const left = Math.min(Math.max(wanted, EDGE), limit);

  return { left, top: flipped ? above : below, flipped };
}

function tip() {
  return document.getElementById(TIP_ID);
}

function place(trigger, element) {
  const rect = trigger.getBoundingClientRect();
  const box = element.getBoundingClientRect();
  const at = chooseTooltipPosition(
    rect,
    { width: box.width, height: box.height },
    { width: window.innerWidth, height: window.innerHeight },
  );
  element.style.left = `${Math.round(at.left)}px`;
  element.style.top = `${Math.round(at.top)}px`;
  element.classList.toggle('flipped', at.flipped);
}

export function hideInfo() {
  const element = tip();
  if (element) {
    element.classList.add('hidden');
    element.textContent = '';
  }
  if (openTrigger) openTrigger.removeAttribute('aria-describedby');
  openTrigger = null;
  openedBy = null;
}

export function showInfo(trigger, how) {
  const element = tip();
  if (!element || !trigger) return;
  const text = trigger.dataset ? trigger.dataset.info || '' : '';
  if (!text) {
    hideInfo();
    return;
  }

  if (openTrigger && openTrigger !== trigger) {
    openTrigger.removeAttribute('aria-describedby');
  }

  element.textContent = text;
  element.classList.remove('hidden');
  openTrigger = trigger;
  openedBy = how || 'hover';
  trigger.setAttribute('aria-describedby', TIP_ID);

  /* Measured after it is visible: a hidden element has no box, and the tip's
     height depends on how the text wraps inside its max-width. */
  place(trigger, element);
}

/**
 * Re-position, or close if the trigger has gone.
 *
 * Adapter cards are rebuilt whenever the *set* of adapters changes, so a tip
 * left open across a hot-plug can end up anchored to a node that is no longer
 * in the document -- which reads as a tooltip stuck in the wrong place rather
 * than as a card being replaced underneath it.
 */
export function refreshInfo() {
  if (!openTrigger) return;
  if (!openTrigger.isConnected) {
    hideInfo();
    return;
  }
  const element = tip();
  if (element) place(openTrigger, element);
}

function triggerFor(target) {
  return target && target.closest ? target.closest('.info[data-info]') : null;
}

/* A touch produces pointerover *and* click, in that order. Without this the tap
   would open on the first and close on the second, so an info icon on a tablet
   would flash and do nothing -- which is precisely the platform this whole
   affordance exists for. */
function isHoverPointer(event) {
  return !event.pointerType || event.pointerType === 'mouse'
    || event.pointerType === 'pen';
}

document.addEventListener('pointerover', (event) => {
  if (!isHoverPointer(event)) return;
  const trigger = triggerFor(event.target);
  if (trigger && trigger !== openTrigger) showInfo(trigger, 'hover');
});

document.addEventListener('pointerout', (event) => {
  if (!isHoverPointer(event)) return;
  const trigger = triggerFor(event.target);
  // Only a hover-opened tip follows the pointer out. One the operator clicked
  // is deliberate and stays until it is dismissed.
  if (trigger && trigger === openTrigger && openedBy === 'hover') hideInfo();
});

document.addEventListener('click', (event) => {
  const trigger = triggerFor(event.target);
  if (trigger) {
    // A click on an already-clicked tip dismisses it, so the mode is escapable
    // without knowing about Escape. A click on a hovered one pins it.
    if (openTrigger === trigger && openedBy === 'click') hideInfo();
    else showInfo(trigger, 'click');
    return;
  }
  // A tap anywhere else dismisses, the same idiom the theme menu uses: a popup
  // that only closes via its own control is a popup people leave open.
  if (openTrigger) hideInfo();
});

document.addEventListener('focusin', (event) => {
  const trigger = triggerFor(event.target);
  if (trigger) showInfo(trigger, 'focus');
  else if (openedBy === 'focus') hideInfo();
});

document.addEventListener('keydown', (event) => {
  if (event.key === 'Escape' && openTrigger) hideInfo();
});

/* A tip anchored to something that has scrolled or been re-laid-out points at
   nothing. Capture, because the scroll may happen in any container. */
document.addEventListener('scroll', refreshInfo, true);
addEventListener('resize', refreshInfo);

/* Leaving a view takes the trigger off screen with it. */
document.addEventListener('rbgc:viewchange', hideInfo);

/* A dialog opens in the top layer, above anything a z-index can reach, so a
   tip left showing under it is both unreadable and unclosable. */
document.addEventListener('rbgc:modalopen', hideInfo);
