"""Minimal TrueType/OpenType metrics reader (advances + vertical metrics).

The web backend measures text **in Python** — the layout/measurement seam
(``measure_text``, ``measure_line_height``, ``font_metrics``) runs synchronously
during ``panel.render()``, long before anything reaches the browser, so the
backend cannot ask the canvas how wide a run is. It predicts the browser's
rendering instead: the browser draws with the *same* bundled Noto faces and
``fontKerning: "none"``, so a run's width is the plain sum of its glyphs'
advance widths — exactly what this reader returns.

Only the tables needed for horizontal metrics are parsed:

* ``head`` — ``unitsPerEm`` (the em square all metrics are expressed in),
* ``hhea`` — ``ascender`` / ``descender`` / ``lineGap`` and ``numberOfHMetrics``,
* ``hmtx`` — the per-glyph ``advanceWidth`` array,
* ``cmap`` — codepoint → glyph id (format 4 for the BMP, format 12 for the
  full Unicode range; the pair covers Latin, kana/kanji, and astral emoji).

This is deliberately from-scratch (``struct`` only, no ``fontTools``) to keep
PuiKit dependency-free, in the same spirit as the Windows backend's hand-rolled
COM. Values are returned as **em fractions** (advance / unitsPerEm) so a caller
scales by the point size once; kerning, ligatures, and shaping are out of scope
(the browser is told to skip them too).
"""

from __future__ import annotations

import struct

# A codepoint with no glyph in the font resolves to glyph 0 (.notdef); its
# advance is what the browser reserves for a "missing glyph" box, so measuring
# it as glyph 0's advance keeps Python and the canvas in agreement.
_NOTDEF = 0


class TrueTypeFont:
    """Horizontal + vertical metrics of one font file, in em fractions."""

    def __init__(self, data: bytes):
        self._data = data
        self.units_per_em = 1000
        self._num_h_metrics = 0
        self._advances: list[int] = []          # advanceWidth per glyph (font units)
        self._cmap: dict[int, int] = {}         # codepoint -> glyph id (BMP, format 4)
        self._cmap12: list[tuple[int, int, int]] = []  # (start, end, startGid) groups
        # Vertical metrics in font units; exposed as em fractions below.
        self._ascender = 800
        self._descender = -200
        self._line_gap = 0
        self._parse()

    # --- public metrics (em fractions) -------------------------------------

    @property
    def ascent(self) -> float:
        """Ascender height as a fraction of the em (baseline up to line top)."""
        return self._ascender / self.units_per_em

    @property
    def descent(self) -> float:
        """Descender depth as a *positive* fraction of the em (baseline down)."""
        return -self._descender / self.units_per_em

    @property
    def line_gap(self) -> float:
        """Leading between lines as a fraction of the em."""
        return self._line_gap / self.units_per_em

    @property
    def line_height(self) -> float:
        """Total line pitch (ascent + descent + line gap), em fraction."""
        return (self._ascender - self._descender + self._line_gap) / self.units_per_em

    def glyph_for(self, codepoint: int) -> int:
        gid = self._cmap.get(codepoint)
        if gid is not None:
            return gid
        for start, end, start_gid in self._cmap12:
            if start <= codepoint <= end:
                return start_gid + (codepoint - start)
        return _NOTDEF

    def advance(self, codepoint: int) -> float:
        """Advance width of the glyph for ``codepoint``, as an em fraction."""
        gid = self.glyph_for(codepoint)
        if not self._advances:
            return 0.0
        if gid >= len(self._advances):
            # Glyphs past numberOfHMetrics all share the last advance (the hmtx
            # format's run-length tail); the array we built already ends there.
            gid = len(self._advances) - 1
        return self._advances[gid] / self.units_per_em

    def advance_text(self, text: str) -> float:
        """Summed advance of ``text`` (no kerning/ligatures), as an em fraction.

        Combining marks (zero-advance glyphs) and variation selectors already
        contribute their own advance from ``hmtx`` (0 for a combining mark), so
        a plain sum matches the browser under ``fontKerning: "none"``."""
        return sum(self.advance(ord(ch)) for ch in text)

    # --- parsing -----------------------------------------------------------

    def _parse(self) -> None:
        data = self._data
        if len(data) < 12:
            return
        num_tables = struct.unpack_from(">H", data, 4)[0]
        tables: dict[bytes, tuple[int, int]] = {}
        pos = 12
        for _ in range(num_tables):
            if pos + 16 > len(data):
                break
            tag = data[pos : pos + 4]
            offset, length = struct.unpack_from(">II", data, pos + 8)
            tables[tag] = (offset, length)
            pos += 16

        if b"head" in tables:
            off = tables[b"head"][0]
            self.units_per_em = struct.unpack_from(">H", data, off + 18)[0] or 1000

        num_h_metrics = 0
        if b"hhea" in tables:
            off = tables[b"hhea"][0]
            self._ascender, self._descender, self._line_gap = struct.unpack_from(
                ">hhh", data, off + 4
            )
            num_h_metrics = struct.unpack_from(">H", data, off + 34)[0]
        self._num_h_metrics = num_h_metrics

        if b"hmtx" in tables and num_h_metrics:
            off = tables[b"hmtx"][0]
            self._advances = [
                struct.unpack_from(">H", data, off + i * 4)[0]
                for i in range(num_h_metrics)
            ]

        if b"cmap" in tables:
            self._parse_cmap(tables[b"cmap"][0])

    def _parse_cmap(self, base: int) -> None:
        data = self._data
        num_tables = struct.unpack_from(">H", data, base + 2)[0]
        best4: int | None = None   # subtable offset for a format-4 BMP map
        best12: int | None = None  # subtable offset for a format-12 full map
        for i in range(num_tables):
            rec = base + 4 + i * 8
            platform, encoding = struct.unpack_from(">HH", data, rec)
            sub_off = base + struct.unpack_from(">I", data, rec + 4)[0]
            if sub_off + 2 > len(data):
                continue
            fmt = struct.unpack_from(">H", data, sub_off)[0]
            # Prefer Windows Unicode subtables; fall back to any Unicode one.
            unicode_like = platform == 3 or platform == 0
            if fmt == 12 and (platform, encoding) in ((3, 10), (0, 4), (0, 6)):
                best12 = sub_off
            elif fmt == 4 and unicode_like and best4 is None:
                best4 = sub_off
        if best4 is not None:
            self._parse_cmap4(best4)
        if best12 is not None:
            self._parse_cmap12(best12)

    def _parse_cmap4(self, off: int) -> None:
        data = self._data
        seg_x2 = struct.unpack_from(">H", data, off + 6)[0]
        seg_count = seg_x2 // 2
        end_off = off + 14
        start_off = end_off + seg_x2 + 2  # +2 skips reservedPad
        delta_off = start_off + seg_x2
        range_off = delta_off + seg_x2
        cmap = self._cmap
        for s in range(seg_count):
            end = struct.unpack_from(">H", data, end_off + s * 2)[0]
            start = struct.unpack_from(">H", data, start_off + s * 2)[0]
            delta = struct.unpack_from(">h", data, delta_off + s * 2)[0]
            range_offset = struct.unpack_from(">H", data, range_off + s * 2)[0]
            if start > end:
                continue
            for cp in range(start, end + 1):
                if cp == 0xFFFF:
                    continue
                if range_offset == 0:
                    gid = (cp + delta) & 0xFFFF
                else:
                    # idRangeOffset points into the glyphIdArray that follows the
                    # idRangeOffset array; the spec's pointer arithmetic in bytes.
                    idx = range_off + s * 2 + range_offset + (cp - start) * 2
                    if idx + 2 > len(data):
                        continue
                    gid = struct.unpack_from(">H", data, idx)[0]
                    if gid != 0:
                        gid = (gid + delta) & 0xFFFF
                if gid != 0:
                    cmap[cp] = gid

    def _parse_cmap12(self, off: int) -> None:
        data = self._data
        n_groups = struct.unpack_from(">I", data, off + 12)[0]
        groups = []
        pos = off + 16
        for _ in range(n_groups):
            if pos + 12 > len(data):
                break
            start, end, start_gid = struct.unpack_from(">III", data, pos)
            groups.append((start, end, start_gid))
            pos += 12
        self._cmap12 = groups


_CACHE: dict[str, TrueTypeFont] = {}


def load(path: str) -> TrueTypeFont:
    """Return the parsed metrics for ``path``, decoding each file only once."""
    font = _CACHE.get(path)
    if font is None:
        with open(path, "rb") as fh:
            font = TrueTypeFont(fh.read())
        _CACHE[path] = font
    return font
