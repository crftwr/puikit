# Drag & Drop

"Drag and drop" is two unrelated features wearing one name. PuiKit keeps them
separate, because they live on different sides of the process boundary and have
very different backend support.

## Two features

### 1. Intra-app drag — *gesture, not capability*

Dragging **inside** the PuiKit window: a file from one pane to another, a list
reorder, a splitter. This is built entirely on the `MOUSE_DRAG` event stream
every mouse-capable backend already emits (the curses backend parses xterm SGR
mouse reporting; see `puikit/backends/curses_backend.py`). It is a widget/Panel
concern — press, track, hit-test the drop target, release — and needs **no
capability flag**. TUI supports it today; the LogView's drag-select is the
existing proof.

### 2. OS-integrated drag-out — *capability `os_drag_drop`*

Dragging a file **out** of the app onto another application (Finder, an editor).
This requires the app to be an OS drag *source*:

- macOS: a native `NSView` adopting `NSDraggingSource`, calling
  `beginDraggingSessionWithItems:event:source:` with file-URL pasteboard items,
  started from the live `mouseDragged:` event.

A terminal app **cannot** do this. In TUI mode the program is a guest speaking a
byte stream to the terminal emulator; the emulator owns the `NSView`, the window,
and any OS drag session. No escape-sequence vocabulary (xterm/OSC/SGR) lets the
inner program initiate a system file drag. So `os_drag_drop` sits under *System
Integration* alongside the native file dialog and system tray — all need a
native window the terminal denies you — and is `False` for TUI.

This is exactly why the reference predecessor, tfm/ttk, can export files by drag
only from its native CoreGraphics backend, never from curses.

## Capability mapping

| Backend       | `drag_and_drop` (drop-in) | `os_drag_drop` (drag-out) |
|---------------|---------------------------|---------------------------|
| TUI (curses)  | False                     | False                     |
| GUI-Web       | True (browser-limited)    | False (no OS drag source) |
| GUI-Desktop   | True                      | **True** (NSDraggingSource) |
| Mobile        | inherits Web (False)      | False                     |
| Game          | False                     | False                     |

`MacOSBackend` implements this: its content view adopts `NSDraggingSource` and
`begin_file_drag` starts a real `beginDraggingSessionWithItems:event:source:`
session, one file-URL `NSDraggingItem` per path, imaged with the Finder icon.
The session begins from the live mouse `NSEvent` the view caches on each
`mouseDown:`/`mouseDragged:`, as AppKit requires.

## The intent API

The app issues one intent and never branches on the backend:

```python
# In a file list's drag handler, on a MOUSE_DRAG that leaves the pane:
started = panel.begin_file_drag(
    selected_paths, event,
    operations=("copy", "move"),
    on_complete=lambda op: paths_moved(selected_paths) if op == "move" else None,
)
```

- `Panel.begin_file_drag(paths, event=None, operations=("copy",), on_complete=None) -> bool`
  resolves the capability.
- On `os_drag_drop` backends it delegates to `Backend.begin_file_drag`, which
  starts a real OS drag session and returns `True`.
- On every other backend — TUI especially — it **falls back to copying the
  paths to the clipboard** (newline-joined) and returns `False`. Not a real
  drag, but the idiomatic terminal substitute: the user pastes the path into
  the target app. The curses clipboard bridges via OSC 52.

This keeps the framework contract intact: the app declares "export these files
by drag," and the backend decides how to realize it.

### Copy vs. move

`operations` is the set the source offers — any of `"copy"`, `"move"`, `"link"`.
The destination app chooses one (Finder picks by modifier key / drop target).

The file bytes are always *copied* to the receiver; a move differs only in that
the **source** must then delete the originals. **PuiKit never deletes files.**
Instead the chosen operation is reported back through `on_complete(op)` once the
session ends (`op` is `"copy"` / `"move"` / `"link"`, or `"none"` if cancelled),
and the *app* performs the move and any undo bookkeeping. This keeps the
consequential deletion in the app layer — where tfm's file-manager logic and
undo already live — rather than buried in the framework.

On macOS this rides the view's `draggingSession:sourceOperationMaskForDraggingContext:`
(the offered mask) and `draggingSession:endedAtPoint:operation:` (the result).
The clipboard fallback is copy semantics, so it reports `"copy"`; a terminal
cannot express a cross-app move.

## Drop-in (`drag_and_drop`)

Receiving files/text dropped *onto* the app is the mirror image and is tracked
separately by `drag_and_drop`. GUI backends register the view as a drag
*destination* and deliver a drop as an event; the terminal's only related
behavior is that many emulators paste a dropped file's path as ordinary text
input (arriving as `KEY`/paste, not a positioned drop). This document covers the
drag-*out* side; drop-in handling is future work.
