"""Keyboard focus traversal shared by every container.

The Panel owns *which* widget holds focus, and ``DrawContext.focused`` resolves
the focus cue down the parent chain (a control lights only when its whole
ancestor chain is focused). This module owns *how* Tab / Shift+Tab move that
focus: one walk that descends into nested containers and, when it runs off a
container's end, reports back so the parent advances to its next child. Only the
Panel root wraps; every container below lets focus escape upward, so focus is
never trapped inside one pane.

A container takes part by mixing in :class:`FocusContainer` and implementing
``focus_children()`` (its focusable direct children, in tab order). Leaf widgets
need nothing — they simply are not ``FocusContainer`` instances, so traversal
lands on them and stops.

The whole protocol is duck-typed on four members (``focus_children``,
``get_focused``, ``set_focused``, ``_focus_moved``), so the Panel — which is not
a Widget — drives the same walk over its top-level slots without subclassing.
"""

from __future__ import annotations

from typing import Any


class FocusContainer:
    """Mixin giving a container boundary-crossing Tab traversal.

    Subclasses store the focused direct child in ``_focused`` and list their
    focusable children in tab order from ``focus_children()``. Everything else
    (descending, escaping, wrapping) is handled by the module functions below.
    """

    #: The focused direct child, or None. Subclasses already keep this field;
    #: it is declared here only so the default accessors below have something
    #: to read.
    _focused: Any | None = None

    #: Whether this container is a focus stop even with no focusable child —
    #: True only for containers that respond to keys on their own (a ScrollView
    #: scrolls with arrows, a Tabs switches with left/right). Pure hosts
    #: (Container, LayoutView) leave it False, so traversal passes them by when
    #: they hold nothing focusable instead of parking on an invisible stop.
    focus_stop_when_empty: bool = False

    def focus_children(self) -> list[Any]:
        """Focusable direct children, in tab order. Must be overridden."""
        raise NotImplementedError

    def get_focused(self) -> Any | None:
        return self._focused

    def set_focused(self, widget: Any) -> None:
        self._focused = widget

    def _focus_moved(self) -> None:
        """Hook called after focus changes within this container (a child took
        focus, or a nested container advanced internally). ScrollView overrides
        it to scroll the newly focused child into view; most containers do
        nothing."""

    def focus_enter(self, direction: int) -> bool:
        """Place focus on this container's entry edge (its first focusable child
        when ``direction > 0``, its last when ``< 0``), descending into nested
        containers. Returns False when it has no focusable descendant."""
        return _enter(self, direction)

    def focus_advance(self, direction: int) -> bool:
        """Move focus to the next focusable after the current child, staying
        inside this container. Returns False when focus runs off the end, so the
        caller advances to its own next child instead."""
        return _advance(self, direction)


def move_focus(container: Any, direction: int, wrap: bool = False) -> bool:
    """Advance focus within ``container``. With ``wrap`` (the Panel root), a step
    off the end re-enters from the opposite edge so focus cycles; without it
    (every nested container) the step returns False so focus escapes upward."""
    if _advance(container, direction):
        return True
    if wrap:
        return _enter(container, direction)
    return False


def focus_on_click(container: Any, widget: Any) -> None:
    """Move focus to a clicked child if it can take focus. Shared by the Panel
    and every container so click-to-focus lives in exactly one place."""
    if getattr(widget, "focusable", False):
        container.set_focused(widget)


def _enter(container: Any, direction: int) -> bool:
    children = container.focus_children()
    order = children if direction > 0 else list(reversed(children))
    for child in order:
        if _land(container, child, direction):
            return True
    return False


def _advance(container: Any, direction: int) -> bool:
    children = container.focus_children()
    if not children:
        return False
    cur = container.get_focused()
    if cur not in children:
        return _enter(container, direction)
    # Let a nested container consume the step internally before we move past it.
    if isinstance(cur, FocusContainer) and cur.focus_advance(direction):
        container._focus_moved()
        return True
    step = children.index(cur) + direction
    while 0 <= step < len(children):
        if _land(container, children[step], direction):
            return True
        step += direction
    return False


def _land(container: Any, child: Any, direction: int) -> bool:
    """Place focus on ``child``. A nested container is entered on the matching
    edge; if it has no focusable descendant it still becomes a stop when it
    responds to keys on its own (``focus_stop_when_empty``, e.g. a scrollable
    view), otherwise it is skipped so focus never parks on an inert host."""
    if isinstance(child, FocusContainer) and not child.focus_enter(direction):
        if not child.focus_stop_when_empty:
            return False
    container.set_focused(child)
    container._focus_moved()
    return True
