"""Load every web-GUI module and call its exports, watching for ReferenceError.

This exists because of a bug the whole text-based web suite missed. Splitting
`app.js` into ES modules left `bannerTimer` declared in `dom.js` while its only
user, `showBanner`, moved to `api.js`. A module is always strict, so the
reference threw `ReferenceError` -- but only when the function *ran*, which is
after a button is pressed.

Nothing detected it. The page loaded, every module parsed, the import graph was
valid, and a browser walk-through of all six views reported zero console errors,
because none of those things call `showBanner`. The first symptom was a saved
setting reporting **"Could not reach the server"** while the request had in fact
succeeded -- the throw lands *after* the banner text is written, so the failure
message was itself the bug rather than a report of one.

Parsing cannot find this: the identifier is syntactically fine. A general
free-variable scan was tried and rejected -- HTML inside template literals gave
it a dozen false positives per run, and a check that cries wolf is worse than
none. Executing the code is what actually answers the question.

Node is used rather than a browser: it takes a second, needs no display, and
ReferenceError is a language-level fault, so a DOM stub is enough to surface it.
"""

from __future__ import annotations

import json
import shutil
import subprocess
import textwrap
from pathlib import Path

import pytest

STATIC = Path(__file__).resolve().parent.parent / "server" / "web" / "static"

pytestmark = pytest.mark.skipif(
    shutil.which("node") is None,
    reason="node is not installed; this check is advisory and skips cleanly",
)


#: A DOM permissive enough that calls reach their own bodies. Anything missing
#: raises TypeError, which is ignored -- only ReferenceError matters here, and
#: that is a fault in the module rather than in this stub.
HARNESS = textwrap.dedent(
    """
    const files = process.argv.slice(2);

    function stub(name) {
      const target = function () { return stub(name); };
      return new Proxy(target, {
        get(_t, prop) {
          if (prop === Symbol.toPrimitive) return () => '';
          if (prop === 'then') return undefined;          // not a thenable
          if (prop === 'length') return 0;
          if (prop === 'classList' || prop === 'dataset' || prop === 'style')
            return stub(name + '.' + String(prop));
          return stub(name + '.' + String(prop));
        },
        set() { return true; },
        has() { return true; },
        apply() { return stub(name + '()'); },
        construct() { return stub('new ' + name); },
      });
    }

    globalThis.document = stub('document');
    globalThis.window = globalThis;
    globalThis.location = stub('location');
    globalThis.WebSocket = function () { return stub('ws'); };
    globalThis.fetch = () => Promise.resolve({ ok: true, json: async () => ({}) });
    globalThis.requestAnimationFrame = () => 0;
    // Bare `addEventListener(...)` at module scope is `window.addEventListener`
    // in a browser. Node has no such global, so without these the import chain
    // fails and every module is reported broken -- a stub gap, not a fault.
    globalThis.addEventListener = () => {};
    globalThis.removeEventListener = () => {};
    globalThis.dispatchEvent = () => true;
    globalThis.CustomEvent = function (type, init) {
      return { type, detail: (init || {}).detail };
    };

    const problems = [];

    // Arguments shaped like the real ones: a status object, a string, a number.
    const ARGS = [
      [],
      [{}],
      ['text', 'good'],
      [{ adapters: [], hardware: [], clients: [], server: {}, video: {},
         datapath: {} }],
      ['a', 'b', 0],
    ];

    for (const file of files) {
      let mod;
      try {
        mod = await import('file://' + file.replace(/\\\\/g, '/'));
      } catch (exc) {
        problems.push({ file, export: '(import)', error: String(exc) });
        continue;
      }
      for (const [name, value] of Object.entries(mod)) {
        if (typeof value !== 'function') continue;
        for (const args of ARGS) {
          try {
            const out = value(...args);
            if (out && typeof out.catch === 'function') out.catch(() => {});
          } catch (exc) {
            if (exc instanceof ReferenceError) {
              problems.push({ file, export: name, error: String(exc) });
            }
          }
        }
      }
    }

    console.log(JSON.stringify(problems));
    process.exit(0);
    """
)


def _run_harness(files: list[Path], tmp_path: Path) -> list[dict]:
    script = tmp_path / "harness.mjs"
    script.write_text(HARNESS, encoding="utf-8")
    result = subprocess.run(
        ["node", str(script), *[str(f) for f in files]],
        capture_output=True, text=True, timeout=120,
        cwd=str(tmp_path),
    )
    assert result.returncode == 0, f"harness failed:\n{result.stderr}"
    line = [ln for ln in result.stdout.splitlines() if ln.startswith("[")]
    assert line, f"harness produced no result:\n{result.stdout}\n{result.stderr}"
    return json.loads(line[-1])


def _modules() -> list[Path]:
    return sorted(STATIC.glob("js/**/*.js"))


def test_every_module_import_succeeds(tmp_path: Path) -> None:
    """A module that fails to import stops the whole page, not just itself."""
    problems = _run_harness(_modules(), tmp_path)
    failed = [p for p in problems if p["export"] == "(import)"]
    assert not failed, "modules failed to import:\n" + "\n".join(
        f"  {Path(p['file']).name}: {p['error']}" for p in failed
    )


def test_no_export_references_an_undeclared_binding(tmp_path: Path) -> None:
    """Calling an export must not raise ReferenceError.

    That is the signature of a binding stranded in another module by the split
    -- see this file's docstring for the one that shipped.
    """
    problems = _run_harness(_modules(), tmp_path)
    refs = [p for p in problems if p["export"] != "(import)"]
    assert not refs, "undeclared bindings reached at runtime:\n" + "\n".join(
        f"  {Path(p['file']).name}:{p['export']} -> {p['error']}" for p in refs
    )
