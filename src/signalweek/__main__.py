"""Enable ``python -m signalweek …`` as an alias for the ``signalweek`` CLI."""

from __future__ import annotations

from signalweek.cli import main

if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
