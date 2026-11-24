# PuiKit Keyboard Contract — Design

Status: **implemented** on macOS, Windows, web, VT (Windows console and POSIX
tty) and curses via the shared helpers `puikit.event.char_key_event` and
`puikit.event.CONTROL_CHAR_KEYS`. The IME focus-gating half (§6) is covered by
`tests/test_text_input_gating.py`. `tests/test_keyboard_conformance.py` holds
the backends side by side: one row per keypress, one column per backend, so a
key one backend reads differently shows up as a row that disagrees.

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
home, end, pageup, pagedown, f1…f12, apps`; `char` is `None`; `modifiers` as
detected.

> **APPS** is the PC keyboard's context-menu (Menu / Application) key —
> `VK_APPS` on Windows. It reaches the Windows backend, the Windows console (VT
> backend), the macOS backend from a PC keyboard (Apple keyboards have none) and
> the web backend. A POSIX terminal sends nothing for it, so an app binds a
> portable key alongside it, per §5 — as it does for the ALT tap below.

> **SPACE** is also a named key (`key="space"`, `char=" "` retained), so
> `Shift-SPACE` is distinguishable from `SPACE` — the same way `Shift-A` differs
> from `a` under §2.

> **ALT tap** — the Windows console (VT backend) additionally delivers
> `key="alt"` for a *bare Alt tap*: Alt pressed and released with no other key
> or mouse press in between — the OS's own menu-activation gesture, intended
> for `MenuBar.open_menu`. It fires on the release, so Alt+X chords never
> produce it. No other backend can see a modifier by itself (a POSIX terminal
> sends nothing at all), so apps must bind a portable activation key (F10)
> alongside it, per §5.

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

Which characters count as printable is `puikit.event.is_typed_char`, shared by
every backend and by `typed_char`: what `str.isprintable()` accepts **plus every
space separator** (Unicode Zs). `isprintable()` admits only the ASCII space among
those, so on its own it dropped the ideographic space (U+3000) a Japanese IME
types for the space bar (xefm#508). Only `" "` becomes the named `space` key of
§1; U+3000 and the rest are glyphs under this rule (`key = char = "　"`).

## 4. Ctrl/Cmd + letter

`key` = lowercase letter, `modifiers ⊇ {"ctrl"}` (or `{"cmd"}`).

One keypress is **one** event. A backend that learns of Ctrl+letter from two
messages (Windows: the keydown, then the control code `TranslateMessage` queues
behind it) delivers it once.

## 4a. Ctrl+I / J / M / H — where GUI and TUI part

A terminal sends a Ctrl+letter as its control code, and four of those are the
very bytes of named keys: Ctrl+I is Tab (0x09), Ctrl+J and Ctrl+M are Enter (LF,
CR), Ctrl+H is Backspace (0x08). Nothing in the byte says which key was pressed.

| Backend | Ctrl+J delivers |
|---|---|
| macOS, Windows, web (GUI) | `key="j"`, `{"ctrl"}` — the letter key is seen |
| Windows console (VT) in Windows Terminal or the classic console window | `key="j"`, `{"ctrl"}` — the letter key, from the virtual key |
| Windows console (VT), any other host | `key="enter"`, `{"ctrl"}` — the named key, Ctrl from key state |
| POSIX tty (VT, curses) | `key="enter"`, `{}` — the named key; a tty cannot report Ctrl |

So on a TUI **the named key wins**, and a binding on Ctrl+I/J/M/H never fires
there: it is Tab, Enter or Backspace instead. An application binds these as
GUI-only, per §5, or warns when such a binding is made for a TUI — the
`ctrl_letter_keys` capability says which side of this line a backend is on.

Windows Terminal and the classic console window (conhost) are the TUIs on the
GUI side, and they report `ctrl_letter_keys` true. Their input records name the
key that was pressed, and a pasted newline arrives as Enter (or, a lone LF in
the classic window, Ctrl+Enter) — never a letter key, so reading the virtual key
cannot turn a paste into Ctrl+J. Any other host feeds ConPTY VT bytes (VS Code,
WezTerm, SSH), and there Ctrl+M and Enter are the same CR.
`PUIKIT_CTRL_LETTERS=1` or `=0` overrides the detection.

Ctrl+[ is not portable either: it is Escape on a terminal and on Windows, and
`[` with Ctrl on macOS and the web. Do not bind it.

The control codes that name keys are one table, `puikit.event.CONTROL_CHAR_KEYS`,
shared by every backend that reads characters. A backend adds only codes its own
platform sends (macOS: keypad Enter 0x03, back-tab 0x19) and never copies one
into the shared table — a terminal that read 0x03 as Enter would make Ctrl+C
press Enter.

## 5. Terminal limits are explicit

curses cannot deliver `cmd`, and arbitrary `alt` / Option + letter combos are
unreliable. A binding that requires them (e.g. `Cmd-ENTER`, `Alt-ENTER`) is
therefore **GUI-only** and simply never fires on the curses backend. Applications
should treat such bindings as GUI-conditional rather than expecting parity across
backends. The **cursor / editing** keys of §5a are the deliberate exception:
their modified forms are decoded on every backend.

> The **Windows console** (VT backend) is a second exception for `Alt+letter`:
> its input records carry real modifier state, and when the console suppresses
> the produced character for an Alt chord the letter is recovered from the
> virtual key — so `key="f", modifiers={"alt"}` arrives reliably there (the
> menu accelerators, xefm#304). On POSIX terminals Alt+letter stays
> unreliable: the ESC-prefix meta encoding is decoded only for the §5a word
> chords (Alt+b / Alt+f / Alt+d / Alt+Backspace); any other ESC-prefixed
> letter falls back to the Escape key, agreeing with curses.

> **CSI u keys.** Some terminals (Ghostty, kitty, WezTerm, and others with the
> fixterms / kitty keyboard encoding) send a chord the legacy bytes cannot
> carry as `ESC [ <code point> ; <mod> u` - Ctrl+` is `ESC [ 96 ; 5 u`. The
> POSIX VT and curses backends decode it to the event a GUI backend gives
> (`key="`"`, `{"ctrl"}`), so such a binding works in those terminals. A
> terminal that sends nothing for the chord (or uses it itself) still cannot
> deliver it.

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
