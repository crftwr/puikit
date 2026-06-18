# PuiKit Focus System — Design

Status: **implemented**. The traversal protocol (`puikit.focus`), the Panel
root that drives Tab / Shift+Tab (§4), the container rewiring (Container,
ScrollView, Tabs — §5), and the focus-aware list/tree selection cue (§6) are
merged, with `tests/test_focus.py` covering both TUI and GUI profiles.

This document describes how PuiKit moves and draws keyboard focus across a tree
of widgets whose containers differ (a plain `Container`, a scrolling
`ScrollView`, a `Tabs` strip), on backends whose capabilities differ. It follows
the framework's rule: widgets state *intent* (they are focusable, they draw a
cue from `DrawContext.focused`); the Panel layer decides *how* focus is
resolved and *where* Tab goes next.

---

## 1. Goals

- One Tab / Shift+Tab mechanism walks the **whole** widget tree, in order,
  crossing container boundaries — focus is never trapped inside one pane.
- Focus **wraps only at the root**, so the window cycles but no nested
  container swallows the traversal.
- Exactly one widget shows a focus cue, resolved down the parent chain (a
  control lights only when its whole ancestor chain is focused).
- A control's focus cue and its *activation* (Space / Enter) behave identically
  on every backend and across every widget.
- No widget branches on backend type or capabilities to take part.

## 2. Two halves: resolution vs. traversal

Focus has two separate concerns, and PuiKit keeps them apart:

- **Resolution** — *which* widget holds focus and *how the cue is drawn*. The
  Panel owns the focused widget; `DrawContext.focused` cascades down
  `draw_child` via a `hints["focused"]` flag, so a child lights its cue only
  when it is its parent's focused child **and** the parent itself is focused.
  This half predates this design and is unchanged.
- **Traversal** — *how Tab moves* the focus. This is what `puikit.focus` adds:
  one walk, driven from the Panel root, that descends into containers and
  escapes upward at their ends.

## 3. The container protocol (`puikit.focus`)

A container takes part by mixing in `FocusContainer` and implementing
`focus_children()` — its focusable direct children, in tab order. The mixin
supplies the rest in terms of a stored `_focused` child:

- `focus_enter(direction)` — place focus on the entry edge (first child if
  `direction > 0`, last if `< 0`), descending into nested containers. Returns
  `False` if there is no focusable descendant.
- `focus_advance(direction)` — move to the next focusable after the current
  child, **staying inside** this container. Returns `False` when focus runs off
  the end, so the caller advances to *its* next child.

Leaf widgets implement nothing: they simply are not `FocusContainer`s, so
traversal lands on them and stops. The whole protocol is duck-typed on four
members (`focus_children`, `get_focused`, `set_focused`, `_focus_moved`), so the
**Panel** — which is not a `Widget` — drives the identical walk over its
top-level slots without subclassing.

The key rule lives in `move_focus(container, direction, wrap)`:

```python
def move_focus(container, direction, wrap=False):
    if _advance(container, direction):   # moved within this subtree
        return True
    if wrap:                             # only the root passes wrap=True
        return _enter(container, direction)
    return False                         # escape upward to the caller
```

A boundary case is folded into `_land`: a `FocusContainer` with **no** focusable
descendant is still a stop when it is itself focusable — e.g. a scrollable
`ScrollView` of static text becomes a tab stop so the keyboard can reach and
scroll it.

## 4. The Panel root

`Panel.dispatch_event` intercepts Tab / Shift+Tab **before** delivering keys to
the focused slot and calls `focus_tab(direction)`, which is just
`move_focus(self, direction, wrap=True)`. Because the Panel exposes the same
four duck-typed members, the recursion treats it like any other container — only
it wraps.

Modal layers are exempt: when a layer is present it owns its events (and its own
internal focus cycling, e.g. a dialog stepping its buttons), so Tab is routed
in, not intercepted.

`focus_on_click(container, widget)` is the single definition of click-to-focus,
shared by the Panel and every container, replacing three copies of the same
"on click, if focusable, take focus" block.

## 5. How each container maps on

- **Container** — `focus_children()` returns its focusable child slots in order;
  the mixin does the rest.
- **ScrollView** — same, plus it overrides `_focus_moved()` to scroll the newly
  focused child into view. Its old internal Tab cycling (which wrapped and
  trapped focus) is gone; traversal is driven from the root.
- **Tabs** — the active tab's content is the single focused child:
  `focus_children()` returns `[active_content]`, so Tab descends into it (and
  through it, if it is itself a container) and escapes at its ends. The strip
  stays a focus stop in its own right — when the content has no focusable, the
  traversal lands on the `Tabs` widget as a leaf, so left/right still switch
  tabs.

## 6. Visual + activation consistency

Two gaps the audit closed:

- **List / tree selection is now focus-aware.** `selected_row_style` (in
  `widgets/base.py`) gives the selected row the full reverse-video highlight
  while the widget holds focus, and the theme's muted `selection_bg` when focus
  is elsewhere — so a list dims its selection exactly like every other control
  dims its cue, instead of always looking active.
- **One definition of "activate."** `ListView` and `TreeView` now route
  Enter/Space through the shared `is_activate` helper (`widgets/_input.py`),
  matching `Button`, `Checkbox`, and `DropDown`. Space activates a selection on
  every backend, whether it arrives as a symbolic key or a printable char.

## 7. Non-goals (for this iteration)

- Spatial (2-D arrow-key) focus movement between panes — Tab order only.
- A focus-trap API for non-modal overlays (modals already trap by owning their
  events).
- Per-widget custom tab orders beyond child declaration order.
