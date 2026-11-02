"""One way to say a public name is going away (CLAUDE.md, "When it cannot be additive").

A warning that does not name both the replacement and the version that
removes the old spelling is not a migration path, so both are required.
"""

from __future__ import annotations

import warnings


def warn_deprecated(what: str, *, use: str, removed_in: str, stacklevel: int = 3) -> None:
    """Warn that ``what`` is deprecated, naming its replacement and the release that drops it.

    ``stacklevel`` defaults to 3 so the warning points at the app's call of the
    public function that calls this, not at PuiKit.
    """
    warnings.warn(f"{what} is deprecated and will be removed in PuiKit {removed_in}; use {use} instead.",
                  DeprecationWarning, stacklevel=stacklevel)
