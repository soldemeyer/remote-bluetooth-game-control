/* Section navigation and the colour scheme.
 *
 * Leaving a section is announced as a DOM event rather than called
 * directly, so this module does not import the video section while the
 * video section imports `activeView` from here -- a cycle that would
 * leave one of the two half-initialised.
 */

'use strict';

import { $ } from './dom.js';

/* ---------- section navigation ---------- */

/* One section at a time. The page used to be a single scroll of five, so the
   thing being looked for was usually off-screen -- and the video preview kept
   polling while the operator was reading the adapter list.
   `activeView()` is what the preview loop asks before it fetches. */

const VIEW_STORAGE_KEY = 'rbgc.view';
export const DEFAULT_VIEW = 'server';

/* The rail went from six tabs to four: Overview's cards moved into the header,
 * and Bluetooth and Clients merged into Controllers.
 *
 * A returning operator's stored view names one of the three that no longer
 * exist, and `showView` used to return silently on a miss -- leaving *no*
 * section active at all. That is a blank page with a working rail and nothing
 * anywhere to say why, on the first load after an upgrade, for everybody.
 *
 * Bumping the storage key would also avoid it, and would throw away the tab
 * they were last on. This keeps the intent instead. */
export const VIEW_ALIASES = {
  overview: 'server',
  adapters: 'controllers',
  clients: 'controllers',
};

let currentView = DEFAULT_VIEW;

export function activeView() {
  return currentView;
}

export function showView(name) {
  const rail = $('rail');
  if (!rail) return;

  const wanted = VIEW_ALIASES[name] || name;
  // By attribute, not by id. The video section carries `id="video-section"`
  // and app.js looks it up; giving it a second identity for navigation would
  // mean taking the first away.
  const target = document.querySelector(`.view[data-view="${wanted}"]`);
  // **Never leave the page with nothing shown.** The alias map above only
  // covers the names retired so far; this covers the next rename too, which
  // is the one nobody will remember to add an alias for.
  if (!target) {
    if (wanted === DEFAULT_VIEW) return;      // no recursion if even that is gone
    showView(DEFAULT_VIEW);
    return;
  }

  currentView = wanted;
  name = wanted;
  // Announced rather than acted on here. The video section listens and
  // stops its preview; this module stays free of a dependency on it.
  document.dispatchEvent(
    new CustomEvent('rbgc:viewchange', { detail: { view: name } }));
  for (const view of document.querySelectorAll('.view')) {
    view.classList.toggle('active', view === target);
  }
  for (const item of rail.querySelectorAll('.rail-item')) {
    const selected = item.dataset.view === name;
    // `aria-current`, not a class alone: the rail is a set of buttons, and a
    // sighted operator sees the highlight while everyone else gets nothing
    // unless the state is in the accessibility tree too.
    if (selected) item.setAttribute('aria-current', 'page');
    else item.removeAttribute('aria-current');
  }
  try {
    localStorage.setItem(VIEW_STORAGE_KEY, name);
  } catch (err) {
    /* Private windows and blocked site data both throw here. The nav still
       works; it just does not survive a reload. */
  }
}

/* Delegated, like every other handler in this file: the rail is built once,
   but binding per button would break the moment it is re-rendered. */
document.addEventListener('click', (event) => {
  const item = event.target.closest('.rail-item, .summary[data-view]');
  if (item && item.dataset.view) showView(item.dataset.view);
});

/* ---------- theme ---------- */

const THEME_STORAGE_KEY = 'rbgc.theme';
const DEFAULT_THEME = 'amber';

export function applyTheme(name) {
  // The default has no [data-theme] block -- it *is* :root -- so the attribute
  // is removed rather than set, or the selector matches nothing and the page
  // keeps whichever scheme was last applied.
  if (name && name !== DEFAULT_THEME) {
    document.documentElement.setAttribute('data-theme', name);
  } else {
    document.documentElement.removeAttribute('data-theme');
  }
  const menu = $('theme-menu');
  if (menu) {
    for (const item of menu.querySelectorAll('.menu-item')) {
      item.setAttribute('aria-checked', String(item.dataset.theme === name));
    }
  }
  try {
    localStorage.setItem(THEME_STORAGE_KEY, name);
  } catch (err) { /* see above */ }
}

export function closeThemeMenu({ restoreFocus = false } = {}) {
  const menu = $('theme-menu');
  const button = $('theme-button');
  const wasOpen = menu && !menu.classList.contains('hidden');
  if (menu) menu.classList.add('hidden');
  if (button) button.setAttribute('aria-expanded', 'false');
  // Only when the keyboard closed it. Pulling focus back after a click would
  // yank it away from whatever the click was actually aimed at.
  if (restoreFocus && wasOpen && button) button.focus();
}

/* The menu items, in document order. */
function themeItems() {
  const menu = $('theme-menu');
  return menu ? Array.from(menu.querySelectorAll('.menu-item[data-theme]')) : [];
}

function menuIsOpen() {
  const menu = $('theme-menu');
  return Boolean(menu) && !menu.classList.contains('hidden');
}

/* Move focus within the menu, wrapping at both ends.
 *
 * `role="menu"` and `role="menuitemradio"` are a promise that arrows work: a
 * screen reader switches to menu navigation on seeing them and stops passing
 * Tab through. Declaring the role without implementing the keys leaves that
 * user with a menu they can open and cannot move around in -- worse than no
 * role at all, because the role is what took Tab away.
 */
function focusItem(index) {
  const items = themeItems();
  if (!items.length) return;
  const wrapped = (index + items.length) % items.length;
  items[wrapped].focus();
}

/* Delegated, like everything else here, and on the document so a click
   anywhere else dismisses the menu -- a popup that only closes via its own
   button is a popup people leave open. */
document.addEventListener('click', (event) => {
  const choice = event.target.closest('.menu-item[data-theme]');
  if (choice) {
    applyTheme(choice.dataset.theme);
    closeThemeMenu();
    return;
  }
  const button = event.target.closest('#theme-button');
  const menu = $('theme-menu');
  if (button && menu) {
    const open = menu.classList.toggle('hidden') === false;
    button.setAttribute('aria-expanded', String(open));
    // Land on the scheme in use, so the arrows start from where the reader is
    // told they are rather than from nothing.
    if (open) {
      const items = themeItems();
      const checked = items.findIndex(
        (it) => it.getAttribute('aria-checked') === 'true');
      focusItem(checked < 0 ? 0 : checked);
    }
    return;
  }
  if (menu && !event.target.closest('#theme-menu')) closeThemeMenu();
});

document.addEventListener('keydown', (event) => {
  if (event.key === 'Escape') {
    closeThemeMenu({ restoreFocus: true });
    return;
  }
  if (!menuIsOpen()) return;

  const items = themeItems();
  const here = items.indexOf(document.activeElement);
  switch (event.key) {
    case 'ArrowDown':
      event.preventDefault();
      focusItem(here + 1);
      break;
    case 'ArrowUp':
      event.preventDefault();
      focusItem(here - 1);
      break;
    case 'Home':
      event.preventDefault();
      focusItem(0);
      break;
    case 'End':
      event.preventDefault();
      focusItem(items.length - 1);
      break;
    default:
      break;
  }
});

/* Applied before anything is shown, so the page never flashes one scheme and
   then repaints in another. */
(function restorePreferences() {
  let theme = DEFAULT_THEME;
  let view = DEFAULT_VIEW;
  try {
    theme = localStorage.getItem(THEME_STORAGE_KEY) || theme;
    view = localStorage.getItem(VIEW_STORAGE_KEY) || view;
  } catch (err) { /* see above */ }
  applyTheme(theme);
  showView(view);
})();
