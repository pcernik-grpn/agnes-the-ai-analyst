"""Alembic revision ids must fit ``alembic_version.version_num``.

Alembic creates that column as ``VARCHAR(32)``. A longer revision id is
accepted by every local check — the file imports, the chain resolves, the
id is a perfectly good string — and then fails at *runtime*, on the
``UPDATE alembic_version SET version_num=...`` that stamps the revision:

    psycopg.errors.StringDataRightTruncation:
    value too long for type character varying(32)

Nothing surfaces that before CI, and when it does surface it presents as
dozens of unrelated PG tests erroring in setup rather than as "your
revision id is too long". The longest id in the tree is exactly 32
characters, so this ceiling has already been reached once by accident.
"""

from __future__ import annotations

import pathlib
import re

VERSIONS = pathlib.Path(__file__).resolve().parent.parent / "migrations" / "versions"
MAX = 32  # alembic_version.version_num is VARCHAR(32)

_REVISION = re.compile(r'^revision: str = "([^"]+)"', re.MULTILINE)


def test_every_revision_id_fits_alembic_version_num():
    too_long = []
    for path in sorted(VERSIONS.glob("[0-9]*.py")):
        m = _REVISION.search(path.read_text(encoding="utf-8"))
        if m and len(m.group(1)) > MAX:
            too_long.append(f"{path.name}: {m.group(1)!r} is {len(m.group(1))} chars")
    assert not too_long, (
        f"Alembic revision ids must be <= {MAX} chars to fit "
        "alembic_version.version_num (VARCHAR(32)); a longer one stamps "
        "fine locally and fails every PG test in CI setup:\n  "
        + "\n  ".join(too_long)
    )
