# UI-Thread Stall Detector

Status: **reference**. A diagnostic built into the event loop: it names the call
stack that was running while the UI thread stopped painting.

The failure it is for does not raise, log, or crash. Something on the UI thread
takes 400 ms — a directory read, a thumbnail decode, a `subprocess` call — and
the app simply stops responding for that long. The user reports "it froze for a
second"; nobody can say where. Historically each one was found by hand, one
operation at a time, long after it shipped.

---

## Turning it on

Off unless the environment asks for it, so it costs a released app nothing:

```bash
PUIKIT_UI_WATCHDOG=1 python -m yourapp      # default 250 ms threshold
PUIKIT_UI_WATCHDOG=500 python -m yourapp    # report anything over 500 ms
PUIKIT_UI_WATCHDOG=off                      # explicitly off (also 0/no/false)
```

The value is read when the backend opens, not at import, so an app may set it
from its own command line in `main()` (xefm's `--ui-watchdog` does exactly that)
long after `import puikit` has run.

Reports go to the `puikit._watchdog` logger at WARNING. An app that routes
`logging` into its own log pane (xefm bridges the `puikit` logger into its sink)
reads them there; otherwise they land wherever the app's logging is configured
to go, or on stderr through `logging.lastResort`.

---

## Reading a report

```
UI thread blocked 312 ms in key 'f5'
  ... 14 outer frames
  xefm/app.py:2140 in _reload_pane    entries = scan_directory(path)
  xefm/dir_scan.py:88 in scan_directory    for entry in os.scandir(path):
UI thread unblocked after 1.2 s in key 'f5'
```

- **The label** is the unit of work: the event being handled (`key 'f5'`,
  `mouse_click`), `animation tick`, or `timer callback`.
- **The stack** is the UI thread's, sampled from the watchdog thread with
  `sys._current_frames()`. The innermost frames are the interesting ones, so
  only the last 12 are kept — the app's dispatch preamble above them is noise.
  One record per frame, because these land in log *panes*, which list records: a
  single record with embedded newlines is one unreadable row.
- **A stall that keeps going is reported again** each time it grows about four
  times longer (250 ms → 1 s → 4 s → …), so a stall that *moves* is visible
  rather than being frozen at whatever the first sample caught.
- **The `unblocked after` line** is logged by the UI thread itself when the unit
  finally ends, and carries the total. It appears only for a unit that was
  already reported.

---

## What is timed

The UI thread brackets each unit of work it does. Three seams cover the loop:

| Seam | Bracketed in |
|---|---|
| One event delivered to the app's handler | `Backend._watch_handler`, applied by each backend's `run_event_loop` / `run_event_loop_iteration` |
| One animation-tick frame | `_run_tick_callbacks` in `backend.py` — every backend's tick dispatch routes through it |
| One `call_later` timer callback | `Backend._watch_callback`, applied by the base and by the native timers (NSTimer, WM_TIMER) |

Brackets nest (a native menu pumps events inside the dispatch that opened it);
only the outermost runs the clock, so a report names the whole unit rather than
the innermost fragment of it. A bracket entered on a thread that is *not* the
watched one is ignored, so a backend pumping ticks off the UI thread cannot
corrupt the count.

## What is deliberately not a stall

Time inside a **nested OS loop the user is driving** is theirs, not the app's:
the UI is not painting because the user is holding a menu open, dragging files
to another application, or reading the editor the app shelled out to. Those
sites hold `Backend._watchdog_paused()`:

- `Backend.suspended()` and its terminal overrides (shell-out)
- native popup menus (`popUpMenuPositioningItem_`, `TrackPopupMenu`)
- the blocking OLE drag session (`DoDragDrop`)

The clock **restarts** rather than resumes when such a loop returns, so a menu
held open for ten seconds is not charged against the code that runs after it.

Also not covered, by construction: anything outside those three seams — work
during `open()`, module import, or an app's startup before the loop begins.

---

## Cost

While off: one `is None` test per bracket, and `wrap_handler` hands the loop back
its own handler, so nothing is allocated and no thread is started.

While on: two attribute writes and a `threading.get_ident()` per unit of work,
plus a daemon thread that wakes at a quarter of the threshold (clamped to
20–500 ms) and, when nothing is running, does one float read per wake. Sampling
the stack costs real work — `traceback.extract_stack` reads source files — but
that happens on the watchdog thread, only after a stall is already confirmed.

---

## Limits

- **CPython only** for the stack: `sys._current_frames` is a CPython facility.
  Elsewhere the report still names the unit and its duration, without frames.
- **Python frames only.** A UI thread blocked inside a long C call shows the
  Python frame that made the call, not what the native code is doing — which is
  normally exactly what you need (the call site), but it will not distinguish
  two slow calls made from the same line.
- **The threshold is a filter, not a truth.** A first render, a font load or a
  cold-cache directory read can legitimately cross 250 ms once. Look for the
  stalls that repeat, or that a user can trigger on demand.
