# PuiKit Memory-Growth Investigation Playbook

Status: **reference**. A field guide to finding and fixing the kinds of memory
growth PuiKit has actually hit — Python-level retention in the widget/panel
layer, and native (ObjC/CoreGraphics/PyObjC-bridge) retention in the macOS
backend that never shows up in Python object counts.

It is written as a runbook: the ladder of techniques to apply in order, the
tools, and worked examples from the leaks that have been fixed. The guiding
principle throughout: **measure, bisect, reproduce in isolation, then fix** —
never guess at a fix from a plausible-looking line of code.

---

## 0. Symptom and first triage

Symptom: RSS grows as you exercise the app (e.g. switching demo-catalog pages),
roughly monotonically, and does not come back down.

Two independent questions decide which half of the ladder you are on:

1. **Is it Python or native?** Compare `len(gc.get_objects())` against process
   RSS over the same workload. If object count grows, it's Python-level. If
   object count is **flat but RSS grows**, it's native (ObjC/CoreGraphics, or
   PyObjC bridge wrappers) — Python's GC can't see it.
2. **Does it plateau?** A bounded cache warming up looks like a leak for a
   while, then flattens. A real leak stays linear. Always run long enough to
   tell the difference (see §5), and report the **slope after warmup**, not the
   total.

Use the headless `MemoryBackend` for Python-level work (fast, deterministic, no
window) and the real `MacOSBackend` for native work.

---

## 1. Drive the app headlessly and sample RSS

You do not need a human clicking through pages. Build the same shell `main()`
builds (a `Panel`, a nav `ListView`, a `LayoutView` content host), then in a
loop call `show_page(i)` → `panel.render()`. For the GUI backend, force a
synchronous draw and pump one event-loop turn so the native render path
actually runs:

```python
with objc.autorelease_pool():
    show(i); panel.render()
    backend._view.display()                              # force drawRect_
    backend.run_event_loop_iteration(noop, timeout_ms=0) # let AppKit settle
```

Measure **current** RSS, not peak. `resource.getrusage(...).ru_maxrss` is a
high-water mark and only ever rises — useless for spotting a plateau. Read the
live value instead:

```python
def rss_kb():
    return int(subprocess.check_output(["ps", "-o", "rss=", "-p", str(os.getpid())]))
```

Wrap each rendered page in `objc.autorelease_pool()` so genuinely transient ObjC
objects drain every iteration and don't masquerade as a leak.

> Caveat: forcing `_view.display()` in a tight loop renders far faster than the
> real 60 Hz app and skips some run-loop housekeeping, so it **amplifies**
> per-call leaks (good) but can also show small transient noise. Confirm any
> finding against a longer, calmer soak (§5).

---

## 2. Localize: bisect by page, then by primitive

Once you can measure a rate, narrow *what* leaks before asking *why*.

- **Per page.** Render each page N times and report KB/render per page. A
  uniform rate across all pages points at shared scaffolding (text, clipping,
  the per-frame fill); a spike on one page points at a specific widget. (Example:
  after the text fix, growth concentrated on Color, Markdown, Menu — all
  many-distinct-string pages — which is what cracked the bridge-wrapper leak.)
- **Per primitive.** No-op one backend draw method at a time and re-measure. Do
  it at the **command-append** boundary (`draw_text`, `fill_rect`, `push_clip`/
  `pop_clip` in matched pairs, …) so the native renderer never sees those
  commands. Disable clip/group in pairs to keep the graphics-state stack
  balanced. The primitive whose removal collapses the rate is your culprit.

`ps`-based RSS is noisy at small signal. Average over enough iterations that the
signal dwarfs the noise (hundreds of renders, tens of MB of expected delta), and
re-run suspicious rows — a single anomalous row is usually noise, not a finding
(we were briefly misled by a `_render_dim` row for a primitive that was never
even called that cycle).

---

## 3. Name the leaking objects: `heap`, `vmmap`, `malloc_history`

When the leak is native (object count flat, RSS up), stop guessing and ask the
OS. These ship with macOS and attach to a live PID of the same user.

- **`vmmap -summary <pid>`** before/after a workload, diffed by region, tells you
  *where*: a named CoreGraphics/CoreText region vs. the general
  `DefaultMallocZone` / `MALLOC_SMALL` (many small retained allocations).
- **`heap <pid>`** prints a live histogram of `COUNT  BYTES  AVG  CLASS_NAME`.
  Snapshot before and after, diff the counts, and the growing class names itself.
  This is the highest-value single tool. Watch especially for PyObjC bridge
  wrappers: **`OC_BuiltinPythonNumber`** and **`OC_BuiltinPythonUnicode`** — a
  Python number/str bridged into an ObjC slot and then retained.
- **`heap -addresses <ClassName> <pid>`** → a list of live instance addresses;
  feed the most recent ones to **`malloc_history <pid> <addr>`** for the
  allocation backtrace. Run the workload under `MallocStackLoggingNoCompact=1`
  (plain `MallocStackLogging=1` can assert/crash under heavy churn). The Python
  frames collapse to `_PyEval_EvalFrameDefault`, but the C/ObjC frames pinpoint
  the selector — e.g. `+[OC_PythonUnicode unicodeWithPythonObject:]` reached via
  `-[NSConcreteAttributedString initWithString:attributes:]` told us the wrappers
  were minted while *drawing attributed strings*, and `NSViewBackingLayer
  drawInContext:` in the same stack told us the view was **layer-backed** (the
  missing ingredient our first repros lacked).

A `heap` count-diff parser (regex the `COUNT … CLASS_NAME` table, diff two
snapshots) is worth keeping around; it turned a week of guessing into one line of
output naming the exact class and rate (`+32/render`, `+20/cycle`).

---

## 4. Reproduce in isolation before fixing

Every fix here was confirmed by a ~30-line standalone AppKit script that
reproduces the leak with **no PuiKit code**, then shows a one-line change makes
it vanish. This both proves the mechanism and guards against a fix that merely
moves the symptom. The decisive trick was varying **one axis** at a time:

- same value reused vs. a **fresh-identity** value each call,
- a cached singleton (`NSColor.blackColor()`) vs. a freshly-built object,
- non-layer-backed (`lockFocus`) vs. **layer-backed** (`setWantsLayer_(True)` +
  `display()`),
- static content vs. content that changes every frame.

The leaks only appeared on the *fresh-identity* / *layer-backed* / *changing*
side, which is exactly what named the root cause.

---

## 5. Confirm with a long soak

Finish with a 5–10 minute soak that cycles the whole app continuously and prints
RSS, `len(gc.get_objects())`, and the backend's own caches (`_style_fonts`,
`_image_cache`, `_attr_cache`, …) every ~30 s. Report the **second-half slope**.
A fixed leak shows RSS reaching a steady state and going flat (the only growth is
one-time cache warmup); the cache sizes stop moving. Keep the `Bash` timeout
under its 600 000 ms ceiling and have the script self-terminate before it.

---

## 6. The leak patterns PuiKit has actually hit

Concrete catalogue, each found with the ladder above and fixed in
`puikit/widgets/busy_indicator.py` / `puikit/backends/macos_backend.py`:

1. **Unregistered animation tick (Python).** A `BusyIndicator` that left the
   layout kept its 60 fps tick registered, pinning the whole detached page tree
   alive *and* driving off-screen re-renders. Fix: a "drawn since last tick"
   liveness flag (`_drawn`) so a tick with no intervening draw self-unregisters.
   Class of bug: **anything a widget registers on the Panel/backend must be
   released when the widget stops being drawn.**

2. **Per-frame native re-decode.** `_render_image` decoded a fresh `NSImage`
   from disk every frame; AppKit's backing-store caches accumulated. Fix: cache
   by path (`_image_cache`).

3. **Leaky AppKit text convenience methods.** `-[NSString
   drawAtPoint:withAttributes:]` / `drawInRect:withAttributes:` /
   `sizeWithAttributes:` leak ~1.5 KB **per call** in this PyObjC stack,
   independent of content or autorelease pool. Fix: route all text draw/measure
   through `NSAttributedString` (`-drawAtPoint:` / `-size`) via `_attr_string`.

4. **Fresh-identity number bridged into an `id` slot.** The kern value in
   `NSKernAttributeName` was recomputed as a new Python float every call; each
   distinct identity became a retained `OC_BuiltinPythonNumber`. Fix: bridge it
   **once** as a shared `NSNumber` (`self._grid_kern`).

5. **Fresh-identity string retained by a layer-backed display list.** Drawing an
   `NSAttributedString` built from a fresh Python str into the layer-backed view
   left the bridged `OC_BuiltinPythonUnicode` in the layer's CG display list;
   re-wrapping widgets (MarkdownView, proportional `_render_flow_text`) minted a
   new one per run per frame. Fix: bounded caches `_attr_cache` / `_width_cache`
   (cap `_ATTR_CACHE_MAX`, clear-on-overflow) behind `_cached_attr_string`, so
   reuse is keyed by *content* and proxy count is bounded by distinct text, not
   frame count.

**General rule for the macOS backend:** in any per-frame render or measure path,
never hand PyObjC a freshly-built Python `str`/number destined for an ObjC object
slot. Reuse a stable bridged object, or cache by value with a bound. See the
[[macos-nsstring-withattributes-leak]] memory for the short version.

---

## 7. Tooling cheat-sheet

| Goal | Tool |
|---|---|
| Python vs native? | `len(gc.get_objects())` vs `ps -o rss=` |
| Live RSS (not peak) | `ps -o rss= -p <pid>` |
| Which VM region grew | `vmmap -summary <pid>` (diff before/after) |
| Which class grew | `heap <pid>` (diff `COUNT … CLASS_NAME`) |
| Allocation backtrace | `heap -addresses <Class> <pid>` + `malloc_history <pid> <addr>` |
| Make stack logging survive churn | `MallocStackLoggingNoCompact=1` |
| Drain transients per frame | `with objc.autorelease_pool():` around each render |
| Headless Python-level runs | `MemoryBackend` |
| Headless native runs | `MacOSBackend` + `_view.display()` + `run_event_loop_iteration` |
