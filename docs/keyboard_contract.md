# PuiKit Keyboard Contract — Design

Status: **implemented** on curses, macOS, and Windows via the shared helper
`puikit.event.char_key_event`. The IME focus-gating half (§7) is covered by
`tests/test_text_input_gating.py`; the per-backend key translation is exercised by
downstream contract tests in the applications that consume it.

This document is the normative spec for keyboard semantics across PuiKit's
backends. A widget or application never branches on backend type to read the
keyboard: every backend delivers the **same** `Event(KEY, …)` shape, and the rules
below fix exactly what it carries for each class of keypress.

A KEY event carries:

- `key` — the **canonical identity** of the key,
- `char` — the **produced glyph**, or `None`,
- `modifiers ⊆ {"shift","ctrl","alt","cmd"}`.

The concatenated names are canonical — `pageup`, not `page_up`; a consumer's
parser adapts its own token spelling to these.

---

## 1. Named non-text keys

`key` ∈ `enter, escape, tab, backspace, delete, insert, up, down, left, right,
home, end, pageup, pagedown, f1…f12`; `char` is `None`; `modifiers` as detected.

> **SPACE** is also a named key (`key="space"`, `char=" "` retained), so
> `Shift-SPACE` is distinguishable from `SPACE` — the same way `Shift-A` differs
> from `a` under §2.

## 2. Letters `a`–`z`

`key` is **always the lowercase letter**; `char` is the literal typed glyph
(`"a"` / `"A"`); `modifiers` includes `"shift"` **iff** the shift form was
produced. So **Shift-A is `key="a", modifiers={"shift"}` on every backend.**
curses *infers* `shift` from an uppercase letter and lowercases `key`; macOS
lowercases `key` while keeping its real shift flag.

## 3. Other printables (digits, punctuation, shifted symbols)

`key = char =` the **literal produced character** (`"?"`, `"@"`, `"="`, `"!"`).
The shifted symbol *is* the identity — a consumer binds `"!"`, never `"Shift-1"`.
**`shift` must NOT appear in `modifiers`** (a GUI backend that knows shift was held
drops it, so `Shift+1` reports `("!", {})` everywhere). `alt` (Option) is **kept**
(it does not change the base glyph); `ctrl` / `cmd` are **kept**.

## 4. Ctrl/Cmd + letter

`key` = lowercase letter, `modifiers ⊇ {"ctrl"}` (or `{"cmd"}`).

## 5. Terminal limits are explicit

curses cannot deliver `cmd`, and arbitrary `alt` / Option + letter combos are
unreliable. A binding that requires them (e.g. `Cmd-ENTER`, `Alt-ENTER`) is
therefore **GUI-only** and simply never fires on the curses backend. Applications
should treat such bindings as GUI-conditional rather than expecting parity across
backends. The **cursor / editing** keys of §5a are the deliberate exception:
their modified forms are decoded on every backend.

## 5a. Modified cursor & editing keys — word granularity

The named cursor/edit keys `left`, `right`, `backspace`, `delete` carry a word
modifier so a widget can offer whole-word caret motion and deletion:

- **`ctrl`** on Windows and terminals, **`alt`** (Option) on macOS and terminals.
  A consumer treats either as "by word" — `modifiers & {"ctrl","alt"}`.

Delivery per backend, so this holds without branching on backend type:

- **macOS** — Option+arrow / Option+Delete reach the field as the raw key with
  `alt` kept; `doCommandBySelector_` re-translates the originating event rather
  than mapping the `moveWordLeft:` / `deleteWordBackward:` selectors by name.
- **Windows** — `Ctrl+Left/Right/Delete` come straight from `WM_KEYDOWN` with
  `ctrl`; `Ctrl+Backspace` arrives as `WM_CHAR` `0x7F` and is mapped to a
  ctrl-modified `backspace`.
- **curses** — modified keys arrive either as xterm CSI sequences
  (`ESC [ 1 ; <mod> <final>`, `ESC [ <n> ; <mod> ~`) or, when ncurses
  pre-assembles them, as extended keycodes (`kLFT5` = Ctrl+Left); both decode
  the xterm modifier parameter (`1 + Shift·1 + Alt·2 + Ctrl·4`). The readline
  meta chords `ESC b` / `ESC f` (word left/right), `ESC d` (delete word
  forward), and `ESC DEL` (Alt+Backspace, delete word back) are accepted too.

`TextEdit` implements this; see `word_bounds` / `is_word_char` in `puikit.text`
for the word unit (an alphanumeric-or-underscore run; whitespace and punctuation
are separators).

---

## 6. Command keys vs. text input — focus-gated IME

A keypress is sometimes a **command** and sometimes **text**; a GUI's IME makes
this sharp (with a CJK source, every keystroke would otherwise start composition,
so single-letter command bindings like `j` / `f` / `v` would compose instead of
dispatch). PuiKit keeps one `Event(KEY, key, char)` (+ `IME_COMPOSITION`) and
gates on **focus**:

- A text-editing widget declares `wants_text_input = True` (`TextEdit`,
  `ComboBox`). The Panel resolves the focused **leaf** each render and, on a
  transition, calls `backend.begin_text_input()` / `end_text_input()`.
- **Text widget focused** → macOS `keyDown` routes through `interpretKeyEvents`
  (insertText / IME / editing commands).
- **Anything else focused** → `keyDown` translates **directly** to a command KEY
  event and does **not** engage the IME, so `j` dispatches even under a Japanese
  input source. curses / Windows / memory backends inherit the no-op default.

`Panel.focused_leaf()` descends from the **top layer** when a modal is open, so a
`TextEdit` inside a pushed dialog engages the IME (the same modal rule as event
dispatch). See also [`focus_system.md`](focus_system.md) for how the focused leaf
is resolved. Covered by `tests/test_text_input_gating.py`.
