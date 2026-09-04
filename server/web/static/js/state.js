/* The last status the server pushed.
 *
 * Its own module because several sections read it and the socket writes it,
 * and an ES import is a read-only binding -- so this cannot be a bare
 * exported `let`.
 */

'use strict';

let latest = null;

export function getLatest() {
  return latest;
}

export function setLatest(status) {
  latest = status;
}
