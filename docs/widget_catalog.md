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
| `Label` | `label.py` | Single-line static text (opt-in `selectable`: drag-select + copy) |
| `TextBlock` | `text_block.py` | Multi-line static text with wrapping (opt-in `selectable`: drag-select + copy) |
| `Button` | `button.py` | Clickable, label-sized (intrinsic), `on_click` |
| `Checkbox` | `checkbox.py` | Boolean toggle |
| `RadioGroup` | `radio.py` | Mutually exclusive choice |
| `DropDown` | `dropdown.py` | Read-only selection; list opens as a `push_layer` popup |
| `ComboBox` | `combo_box.py` | Editable drop-down: type-to-filter list + free text |
| `TextEdit` | `text_edit.py` | Single-line editable text; full IME/composition, mouse + keyboard selection, clipboard copy/cut/paste |
| `ProgressBar` | `progress_bar.py` | Read-only determinate ratio bar |
| `BusyIndicator` | `busy_indicator.py` | Indeterminate activity spinner (`animation` fallback) |
| `Splitter` | `splitter.py` | Two panes with a draggable divider (drag to resize) |
| `ListView` | `list.py` | Scrollable selectable rows; text by default, or a widget per row via `row_factory` |
| `LogView` | `log_view.py` | Virtualized append-only stream; per-line color, wrap, drag-select + copy, tail-follow |
| `MarkdownView` | `markdown_view.py` | Scrolling read-only Markdown viewer; proportional prose + monospace/sized headings, clickable links, images |
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

#### ProgressBar + BusyIndicator — **shipped**
Read-only ratio display, split into two intents instead of one widget.
- **Existing flexibility?** Partly — a determinate bar is close to a styled,
  non-interactive `Slider`. With no `Slider` yet, `ProgressBar` ships as its own
  small value-only widget (painted like `ScrollBar` — pill on vector backends,
  cell fills on a grid); a caption rides in a sibling `Label`.
- **Resolved:** the *determinate* and *indeterminate* cases were separated.
  `ProgressBar` carries a value (0..1); the indeterminate "busy but unmeasured"
  case became its own `BusyIndicator`, because only it needs the `animation`
  capability and a still-backend fallback — folding it into a value-bearing bar
  would have made the bar carry machinery it has no value for.
- **Abstraction value:** `BusyIndicator` is the clean `animation` test: it
  drives its own per-frame ticks via `panel.request_animation_ticks` on capable
  backends (GUI), and on a still backend (TUI) derives its frame from the wall
  clock — advancing on any other re-render — so it never branches on the backend.
- **Verdict:** Built as two widgets. Revisit folding the determinate bar into a
  `Slider(readonly=True)` mode if/when `Slider` lands.

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

#### ComboBox (editable dropdown) — **shipped**
Type-to-filter / free-text entry over a list.
- **Existing flexibility?** The call was whether to extend `DropDown` with an
  `editable`/`filter` flag or add a class. It shipped as its **own class** that
  *composes* the existing parts rather than re-deriving them: an embedded real
  `TextEdit` owns the editing (cursor, IME composition, horizontal scroll) and
  the floating list reuses `DropDown`'s `push_layer` popup pattern. A flag on
  `DropDown` would have grown that read-only control two divergent modes; a thin
  composing widget kept each part single-purpose.
- **Abstraction value:** Moderate but real — while the popup is open it is the
  modal layer, yet it *forwards* the editing keys back to the field underneath,
  so typing filters the list live (IME and free text included).
- **Verdict:** Built as a composing widget, not a `DropDown` mode.

#### Splitter / resizable pane handle — **shipped**
A draggable divider between two panes.
- **Existing flexibility?** The earlier verdict was "layout/Panel layer, not a
  leaf." It shipped instead as a **self-contained two-pane widget** (`Splitter`,
  a focus container hosting `first`/`second` plus a draggable handle) rather than
  an interactive mutator of the declarative layout's weights. The deviation was
  deliberate: a leaf split pane is reusable anywhere a widget goes (inside a
  list row, a dialog, a tab), needs no new Panel/layout vocabulary, and isolates
  the **drag** axis cleanly (`MOUSE_DRAG` → fraction, clamped to per-pane
  minimums). A layout-weight-mutating divider is still worth building later for
  splits declared in `set_layout`; the two can coexist.
- **Abstraction value:** High and directly tfm-relevant (dual-pane resize); the
  first widget to exercise drag.
- **Verdict:** Built as a leaf composite. Revisit a layout-level interactive
  divider separately.

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

#### Rich text / Markdown — **shipped** (`MarkdownView`)
Inline mixed styles, links, flowing content.
- **Existing flexibility?** `TextBlock` wraps and `Style.font` carries per-run
  faces, but the missing piece was a **styled-run model** (intra-line mixed
  styles + link hrefs). `MarkdownView` introduces it and uses it to render
  Markdown: proportional prose vs. monospace code, per-level sized headings,
  block quotes/lists/rules, fenced code, **clickable hyperlinks**
  (`Panel.open_url`, new `os_open` capability, clipboard fallback on TUI, a
  `pointer` cursor over a link's hit rect), and **block images** (sized to aspect
  ratio via `aspect_extent`, alt glyph on TUI). Virtualized over variable row
  heights.
- **Outcome:** the styled-run model now lives in `markdown_view.py`; if a second
  consumer appears, lift it (span list + wrap) into a shared helper and let
  `TextBlock` opt in, rather than duplicating it.

##### Future work (TODO), roughly in priority order
1. **Selection + copy.** `MarkdownView` cannot select text yet, unlike
   `LogView` / `TextBlock` (`_selection.py`: drag-select + `Cmd`/`Ctrl`+`C`).
   Reuse that machinery so a reader can copy passages. *(Highest-value gap.)*
2. **GitHub-flavored blocks.** Tables (`| a | b |`) and task lists (`- [ ]`) —
   tables likely reuse a future `Table`/`ListView` column helper (§3.2).
3. **More inline runs.** Strikethrough (`~~text~~`, GFM), autolinks (`<url>` and
   bare `https://…`), and reference-style links (`[text][ref]` + `[ref]: url`
   defs) — only inline `[text](url)` is parsed today. Small, self-contained
   additions to `_scan_inline` / `parse_markdown`.
4. **Inline images.** Only standalone `![alt](url)` lines are blocks today;
   support an image *run* inside a wrapped paragraph (a row whose height is the
   tallest run, image or text).
5. **Code-block polish.** A continuous background fill behind the whole block
   (not just per-glyph `bg`), and optional syntax highlighting.
6. **More block nesting.** Nested block quotes, multi-line blockquote flow (each
   `>` line is its own semantic line today, so a quoted paragraph doesn't
   reflow), lists inside quotes, and setext headings (`===` / `---` underline
   form).
7. **Hard line breaks.** A trailing two-spaces or `\` should force a break;
   paragraph lines are always joined with a single space today.
8. **Intra-document anchors.** `[jump](#section)` scrolls the view to a heading.

*Done since the initial ship:* link hover affordance — a `pointer` cursor is set
over a link's hit rect (`MarkdownView.draw`, `set_cursor`).

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

Shipped since this doc was first written: **ProgressBar**, **BusyIndicator**,
**ComboBox**, **Splitter**, **LogView**, and **MarkdownView** (see §1 and the
resolved entries in §3); `MarkdownView` carries open future work in §3.3.

- **To exercise the abstraction layer:** build **Slider** next — the one small
  widget still missing that isolates drag *and* cross-axis intrinsic sizing
  together (the determinate `ProgressBar` could then fold into a
  `Slider(readonly=True)` mode). `BusyIndicator` already covers the `animation`
  fallback story.
- **To unblock tfm:** the dual-pane resize is covered by the new **Splitter**;
  prioritize the **Table** (or a `ListView` column helper) next, and revisit a
  *layout-level* interactive divider for splits declared in `set_layout`.
- **Resist adding:** SpinBox, Tooltip, Accordion — each is reachable by
  configuring or combining what already exists. Add them only if real apps keep
  re-deriving the same glue.
