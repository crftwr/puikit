# PuiKit Widget Catalog — Existing and Potential

Status: **part inventory, part design backlog.** The "Existing widgets"
section describes shipped widgets (`puikit/widgets/`). The "Potential widgets"
section is a backlog of ideas, each scored against PuiKit's design priorities —
it is *not* a commitment to build any of them.

This document exists to answer one recurring question: *do we add a new widget,
or is the need already met?* PuiKit's whole premise is that apps describe intent
and the framework resolves it across backends. A new widget is a new piece of
that vocabulary and a new thing to maintain on every backend forever. So before
adding one we ask the questions in §2.

---

## 1. Existing widgets

One implementation each, running unchanged on every backend.

| Widget | Module | Role |
|--------|--------|------|
| `Widget` | `base.py` | Base class: geometry, `measure`, draw, event, focus |
| `Container` | `container.py` | Groups children; layout host |
| `Label` | `label.py` | Single-line static text |
| `TextBlock` | `text_block.py` | Multi-line static text with wrapping |
| `Button` | `button.py` | Clickable, label-sized (intrinsic), `on_click` |
| `Checkbox` | `checkbox.py` | Boolean toggle |
| `RadioGroup` | `radio.py` | Mutually exclusive choice |
| `DropDown` | `dropdown.py` | Read-only selection; list opens as a `push_layer` popup |
| `TextEdit` | `text_edit.py` | Single-line editable text, full IME/composition |
| `ListView` | `list.py` | Scrollable selectable rows; text by default, or a widget per row via `row_factory` |
| `TreeView` / `TreeNode` | `tree.py` | Expandable hierarchical rows with indentation |
| `Tabs` | `tabs.py` | Title strip swapping a content pane |
| `MenuBar` / `MenuPopup` | `menu.py` | Widget-rendered menu fallback (non-`native_menus` backends) |
| `MessageBox` | `message_box.py` | Modal alert/confirm via `push_layer` (shadow + dim_below) |
| `ScrollBar` | `scroll_bar.py` | Backend-fixed-width thumb/track |
| `ScrollView` | `scroll_view.py` | Scrollable viewport; cycles focusable children |
| `ImageView` | `image.py` | Image on GUI; text/no-op fallback on TUI |
| `LayoutView` | `layout_view.py` | Demo host for the layout system |

---

## 2. How we decide whether to add a widget

In priority order. A "yes" to an earlier question usually means **don't** add a
new widget.

1. **Can existing flexibility already do it?** If the need is met by
   *configuring* an existing widget (a new hint, a style, a mode flag) or by
   *combining* existing widgets in a `Container`/layout, do that instead. A
   `SpinBox` is a `TextEdit` between two `Button`s; an "editable dropdown" might
   be a `DropDown` configuration rather than a new class. Prefer composition and
   configuration over a new entry in the vocabulary.
2. **Does it belong at the widget layer at all?** Some needs are really
   *layout/Panel* concerns (a resizable splitter mutates layout weights; a
   tooltip is a `push_layer` policy). Those may be better as Panel/layout
   features than as leaf widgets.
3. **Does it isolate a real backend-abstraction question?** The widgets worth
   building teach the framework something — a new capability-fallback path, a
   new intrinsic-sizing case, a drag interaction. If it exercises an axis we
   haven't, that's a point in favor.
4. **Does the first real user (tfm) need it?** A dual-pane file manager's needs
   (a splitter, a multi-column table) outrank speculative widgets.
5. **Is the new surface area worth the perpetual cost?** Every widget must be
   correct on TUI *and* every GUI backend, forever.

The rest of this document records candidate widgets and how they score against
these questions — especially question 1.

---

## 3. Potential widgets

### 3.1 High value — fill obvious holes

#### Slider
Continuous or stepped value chosen by dragging a thumb.
- **Existing flexibility?** No. No current widget represents a continuous value
  along a track.
- **Abstraction value:** High. Intrinsically sized on the cross axis (a
  backend-fixed track thickness, like `ScrollBar`) and weighted on the main
  axis. TUI draws `◄──●──►` and moves on arrow / `h`/`l`; GUI gives a draggable
  thumb with hover + `MOUSE_DRAG`. Good unification test for drag.
- **Verdict:** Strong candidate. Small, isolates one axis.

#### ProgressBar
Read-only ratio display; determinate or indeterminate.
- **Existing flexibility?** Partly — a determinate bar is close to a styled,
  non-interactive `Slider`. Worth considering whether `Slider(readonly=True)`
  covers the determinate case before adding a class.
- **Abstraction value:** High for the indeterminate case: it needs the
  `animation` capability and demonstrates clean fallback (GUI animates; TUI
  shows a static fill or marquee-by-tick).
- **Verdict:** Build, but first check it isn't just a `Slider` mode.

#### SpinBox / NumberInput
Numeric entry with +/- steppers and clamping.
- **Existing flexibility?** **Largely yes** — this is `TextEdit` + two `Button`s
  in a `Container`, with numeric validation. The open question is whether the
  validation/clamping logic deserves a reusable class or stays app-side.
- **Abstraction value:** Low-moderate; stresses intrinsic width (sized to digit
  count) but introduces no new backend path.
- **Verdict:** Prefer composition first. Promote to a widget only if several
  apps re-implement the same stepper+validation glue.

### 3.2 Medium — real need, more design surface

#### ComboBox (editable dropdown)
Type-to-filter / free-text entry over a list.
- **Existing flexibility?** This is the key call: is it a **new mode of
  `DropDown`** (add an `editable` / `filter` configuration) or a new widget? It
  shares the popup-list machinery with `DropDown` and the editing machinery with
  `TextEdit`. Strong preference: extend `DropDown` rather than add a class.
- **Abstraction value:** Moderate (combines IME editing with a floating popup).
- **Verdict:** Pursue as a `DropDown` configuration first.

#### Splitter / resizable pane handle
A draggable divider that mutates layout weights.
- **Existing flexibility?** No — but it is **not a leaf widget.** It belongs at
  the layout/Panel layer (your dividers already live there; this makes them
  interactive). Build it there, not in `widgets/`.
- **Abstraction value:** High and directly tfm-relevant (dual-pane resize).
- **Verdict:** Pursue as a layout/Panel feature.

#### Table / DataGrid
Multi-column rows with headers and per-column widths/alignment.
- **Existing flexibility?** Partly — `ListView`'s `row_factory` already makes
  each row an arbitrary widget, so a row of column widgets approximates it. The
  open question is whether column intrinsic-sizing/alignment is common enough to
  standardize rather than re-derive per app.
- **Abstraction value:** High; column intrinsic sizing is a meaty layout case.
  Directly relevant to tfm's name/size/date file list.
- **Verdict:** Likely build (tfm needs it), but explore "`ListView` + column
  layout helper" before a monolithic widget.

#### Tooltip
Hover-triggered transient popup.
- **Existing flexibility?** It's a `push_layer` + `hover` *policy*, not really a
  standalone widget. Consider a Panel-level tooltip hint on any widget rather
  than a `Tooltip` class.
- **Abstraction value:** Low-moderate; reuses layering + hover. Pure on `hover`
  backends; TUI no-ops or routes to a status line.
- **Verdict:** Pursue as a Panel hint, not a leaf widget.

### 3.3 Lower priority / defer

#### Rich text
Inline mixed styles, links, flowing content.
- **Existing flexibility?** `TextBlock` already wraps, and `Style.font` already
  carries per-run faces. The real prerequisite is a **styled-run model**; once
  that exists, rich text may be a `TextBlock` capability rather than a new
  widget.
- **Abstraction value:** High but large; mostly matters for content-heavy apps,
  not a file manager.
- **Verdict:** Defer. Define the styled-run model first, then fold into
  `TextBlock` if possible.

#### Accordion / collapsible panel
- **Existing flexibility?** **Yes** — `Tree` (disclosure) and `Tabs` (swapping)
  already cover the need. No new widget.

#### Date / color pickers
- **Existing flexibility?** No, and they are very backend-divergent (GUI wants
  native pickers; TUI wants a grid widget).
- **Verdict:** Defer until an app actually needs one; likely a strong
  `native_*`-capability fallback story when it happens.

---

## 4. Current recommendation

- **To exercise the abstraction layer:** build **Slider** next (smallest widget
  that isolates drag + cross-axis intrinsic sizing), then **ProgressBar** (after
  confirming it isn't just a `Slider` mode) for the `animation` fallback story.
- **To unblock tfm:** prioritize the **Splitter** (layout-layer) and **Table**
  (or a `ListView` column helper).
- **Resist adding:** SpinBox, ComboBox, Tooltip, Accordion — each is reachable
  by configuring or combining what already exists. Add them only if real apps
  keep re-deriving the same glue.
