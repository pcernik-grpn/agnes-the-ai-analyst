# WAL recovery runbook

Operator playbook for a `system.duckdb` WAL-replay failure. Use this when
Agnes fails to start and the application log contains the signature below.

---

## 1. Detection

### Log signature

Look for any of these strings in `docker logs agnes-app-1`:

```
INTERNAL Error: Failure while replaying WAL file:
Calling DatabaseManager::GetDefaultDatabase with no default database set
```

or

```
INTERNAL Error: ReplayAlter failed
```

Both are caught by `_try_open_system_db` in `src/db.py` as the WAL-replay
error class. Any other `duckdb.Error` (e.g. `IO Error: file is locked`) is
**not** handled by auto-recovery and propagates unchanged.

### Verify the symptom

```bash
docker logs agnes-app-1 2>&1 | grep -E "WAL replay|ReplayAlter|GetDefaultDatabase"
```

A hit confirms you are in the WAL-recovery scenario. No hit means a different
failure class — do not proceed with this runbook.

---

## 2. What the app does automatically

Recovery in `_try_open_system_db` (`src/db.py`) is layered:

**Step A — WAL salvage (common path)**

1. The unreplayable `.wal` file is moved to
   `<STATE_DIR>/system.duckdb.wal.discarded.<unix-ts>` (chmod `0o600`).
2. The DB file itself is reopened at its last checkpoint. This preserves
   all rows up to that checkpoint — typically seconds behind, not hours.
3. On success the app starts normally. Look for:
   ```
   WAL replay failed (…) — discarded the unreplayable WAL and reopened
   system.duckdb at its last checkpoint. Transactions written since that
   checkpoint are lost; admin state up to the checkpoint is intact.
   ```

**Step B — pre-migrate snapshot restore (fallback)**

Only reached when the DB file itself will not open after the WAL is discarded.

1. The broken DB is moved to `<STATE_DIR>/system.duckdb.broken.<unix-ts>`
   (chmod `0o600`).
2. `<STATE_DIR>/system.duckdb.pre-migrate` is inspected read-only via
   `_peek_schema_version` to confirm its `schema_version.version` matches the
   running binary's `SCHEMA_VERSION` (currently `123`, in `src/db.py`).
3. If the versions match, the snapshot is copied in as the new
   `system.duckdb` and the migration ladder re-runs (idempotent). App starts.
4. If the versions do **not** match, auto-recovery is refused with:
   ```
   REFUSING auto-recovery: pre-migrate snapshot stale
   (snapshot v<N>, target v<M>). Auto-recovery would re-run the migration
   ladder and silently drop all rows added since v<N>. Broken DB preserved
   at <path>; ...
   ```
   This is the data-loss mode described in §4.

**When does the pre-migrate snapshot exist?**

`_ensure_schema` (`src/db.py`) copies the DB file to
`<STATE_DIR>/system.duckdb.pre-migrate` with `shutil.copy2` before running
any migration step (only when `current_schema_version > 0`). The copy is
taken after a `CHECKPOINT` to flush pending WAL writes into the main file.
A post-migration `CHECKPOINT` runs after every upgrade to minimize the window
in which an uncommitted `ALTER TABLE` can sit unresolved in the WAL.

**`system.duckdb.pre-migrate` is frozen at the last migration, never
refreshed afterwards (#379/#380).** `refresh_rolling_snapshot` (`src/db.py`)
maintains a separate, rolling-refreshed artifact —
`<STATE_DIR>/system.duckdb.rolling-snapshot/`, a logical `EXPORT DATABASE`
(Parquet + `schema.sql`) taken over the app's own `system.duckdb` connection
on a configurable cadence (`backups.rolling_snapshot_interval_hours`, default
6h, `0` disables; DuckDB-backend only — a no-op on a Postgres-state
instance). It is usually much fresher than `pre-migrate`, but it is a
**manual-recovery aid only** — `_try_open_system_db`'s auto-restore below
still only ever considers `system.duckdb.pre-migrate`. See Option A2 in §4
for how to restore from it.

---

## 3. Manual recovery — Step A failed, Step B succeeded (no data loss)

When Step B auto-recovery completes the app restarts cleanly. Confirm:

```bash
docker logs agnes-app-1 2>&1 | grep "WAL replay failed"
# Expected: "auto-restoring from pre-migrate snapshot …"

docker exec agnes-app-1 /usr/local/bin/python3 -c \
  "from src.db import get_system_db, get_schema_version; \
   conn = get_system_db(); print('schema_version:', get_schema_version(conn))"
# Expected: schema_version: 123
```

Verify row counts are reasonable (see §6).

---

## 4. Manual recovery — auto-recovery refused (snapshot version mismatch)

This happens in two scenarios:

| Direction | Meaning | Risk |
|-----------|---------|------|
| `stale` (snapshot v < binary v) | App was upgraded; snapshot predates the latest migration | Restoring would re-run the ladder and silently drop rows added since `v<snapshot_ver>` |
| `future` (snapshot v > binary v) | Binary was rolled back after a migration already ran | Restoring would leave DB at a version newer than the binary expects (split-brain) |

In both cases the broken DB is preserved at
`<STATE_DIR>/system.duckdb.broken.<unix-ts>` and the snapshot at
`<STATE_DIR>/system.duckdb.pre-migrate`. **Neither file is deleted.**

### Recovery options (ranked by data preservation)

#### Option A — inspect the broken DB read-only and salvage rows

The broken file is the *most recent* copy. Open it without starting the app:

```bash
STATE_DIR=$(docker inspect agnes-app-1 \
  --format '{{range .Mounts}}{{if eq .Destination "/data-state"}}{{.Source}}{{end}}{{end}}')
# Falls back to /data/state if STATE_DIR is the nested layout A:
# STATE_DIR=$(docker inspect agnes-app-1 \
#   --format '{{range .Mounts}}{{if eq .Destination "/data"}}{{.Source}}{{end}}{{end}}')/state

BROKEN=$(ls -1t ${STATE_DIR}/system.duckdb.broken.* | head -1)
echo "Broken DB: ${BROKEN}"

# Open read-only and inspect
docker run --rm -v "${STATE_DIR}:/state:ro" \
  --entrypoint /usr/local/bin/python3 \
  $(docker inspect agnes-app-1 --format '{{.Config.Image}}') \
  -c "
import duckdb, sys
conn = duckdb.connect('/state/$(basename ${BROKEN})', read_only=True)
print('schema_version:', conn.execute('SELECT MAX(version) FROM schema_version').fetchone()[0])
print('users:', conn.execute('SELECT count(*) FROM users').fetchone()[0])
print('table_registry:', conn.execute('SELECT count(*) FROM table_registry').fetchone()[0])
"
```

If the broken DB opens cleanly and row counts look right, export tables to
parquet and import them into a clean DB:

```bash
# Export every table to parquet (run inside the container or adjust image)
docker run --rm -v "${STATE_DIR}:/state" \
  --entrypoint /usr/local/bin/python3 \
  $(docker inspect agnes-app-1 --format '{{.Config.Image}}') \
  -c "
import duckdb, pathlib
broken = '/state/$(basename ${BROKEN})'
out   = '/state/salvage'
pathlib.Path(out).mkdir(exist_ok=True)
conn  = duckdb.connect(broken, read_only=True)
tables = [r[0] for r in conn.execute(
  \"SELECT table_name FROM information_schema.tables WHERE table_schema='main'\"
).fetchall()]
for t in tables:
    conn.execute(f\"COPY {t} TO '{out}/{t}.parquet' (FORMAT PARQUET)\")
    print('exported', t)
"

# Import into a fresh DB (app stopped)
docker run --rm -v "${STATE_DIR}:/state" \
  --entrypoint /usr/local/bin/python3 \
  $(docker inspect agnes-app-1 --format '{{.Config.Image}}') \
  -c "
import duckdb, pathlib, os, shutil
salvage = '/state/salvage'
fresh   = '/state/system.duckdb.recovered'
conn    = duckdb.connect(fresh)
conn.execute(\"SET TimeZone='UTC'\")
for p in sorted(pathlib.Path(salvage).glob('*.parquet')):
    t = p.stem
    conn.execute(f\"CREATE TABLE {t} AS SELECT * FROM read_parquet('{p}')\")
    print('imported', t)
# Flush the WAL into the .recovered file and close cleanly — the cp below
# copies the single file only, so rows still sitting in a companion
# .recovered.wal would be silently left behind.
conn.execute('CHECKPOINT')
conn.close()
"

# Move recovered DB into place (app still stopped)
cp "${STATE_DIR}/system.duckdb.recovered" "${STATE_DIR}/system.duckdb"
```

Then start the app and verify (see §6).

#### Option A2 — restore from the rolling snapshot (`system.duckdb.rolling-snapshot/`)

If Option A's broken-DB salvage isn't possible (the broken file itself won't
open at all), check for a rolling snapshot **before** falling back to Option
B's pre-migrate restore. `system.duckdb.pre-migrate` is frozen at the last
migration transition (`applied_at`, `SCHEMA_VERSION`); `system.duckdb.rolling-
snapshot/` is refreshed on a cadence (`backups.rolling_snapshot_interval_hours`,
default 6h — see `config/instance.yaml.example`) and is usually far more
recent (#380). It is a logical `EXPORT DATABASE` — a directory of Parquet
files plus `schema.sql` — not a single DuckDB file, so restoring it is an
`IMPORT DATABASE` into a fresh file rather than a plain `cp`:

```bash
# Check the snapshot's age before trusting it — an operator can also always
# `docker exec agnes-app-1 stat /data-state/system.duckdb.rolling-snapshot`
ls -la ${STATE_DIR}/system.duckdb.rolling-snapshot/ 2>/dev/null

docker run --rm -v "${STATE_DIR}:/state" \
  --entrypoint /usr/local/bin/python3 \
  $(docker inspect agnes-app-1 --format '{{.Config.Image}}') \
  -c "
import duckdb
fresh = duckdb.connect('/state/system.duckdb.recovered')
fresh.execute(\"IMPORT DATABASE '/state/system.duckdb.rolling-snapshot'\")
print('schema_version:', fresh.execute('SELECT MAX(version) FROM schema_version').fetchone()[0])
print('users:', fresh.execute('SELECT count(*) FROM users').fetchone()[0])
# Flush the WAL into the .recovered file and close cleanly — the cp below
# copies the single file only, so imported rows still sitting in a companion
# .recovered.wal would be silently left behind.
fresh.execute('CHECKPOINT')
fresh.close()
"

# Move recovered DB into place (app still stopped)
cp "${STATE_DIR}/system.duckdb.recovered" "${STATE_DIR}/system.duckdb"
```

**If `system.duckdb.rolling-snapshot/` is missing, also look for
`system.duckdb.rolling-snapshot.prev/`.** `.prev` is normally swap scratch that
exists for milliseconds, but if a refresh's swap failed *and* its restore
failed too, the last good snapshot is preserved there under that name until the
next refresh moves it back. It is a complete, importable export — use it
exactly as above.

The snapshot directory is `0o700` and its files `0o600`, owned by the app's
user: it is a full logical copy of `system.duckdb`, password hashes and
personal-access-token rows included. Read it as that user (or root) — the
`docker run` recipe above already does.

**This is a manual step only** — `refresh_rolling_snapshot` (`src/db.py`) does
not participate in `_try_open_system_db`'s auto-recovery; the WAL-replay
auto-restore in §2 still only ever considers `system.duckdb.pre-migrate`.

**Search indexes rebuild themselves — with one caveat for offline hosts.**
The snapshot is a real `EXPORT DATABASE`, including the DuckDB FTS artifacts
`PRAGMA create_fts_index` builds over `knowledge_items` and `glossary_terms`
(`src/fts.py`) — `fts_main_knowledge_items` / `fts_main_glossary_terms`,
each its own schema of tables plus an extension-generated `match_bm25`
macro. `IMPORT DATABASE` replays that macro's `CREATE MACRO` statement,
which needs DuckDB to resolve the `fts` extension's functions; if the
extension isn't already loaded on the host running the import, DuckDB
auto-installs it on the spot (the same autoload it uses for
`read_json`/spatial/etc.) — invisible on a host with normal network access,
but on an offline/air-gapped recovery host that auto-install fails, and
because `IMPORT DATABASE` runs the whole `schema.sql` as one transaction,
that single failing statement aborts the *entire* import — no table comes
back, not just search. If `IMPORT DATABASE` fails with an extension-download
error, import from a copy of the snapshot with the FTS lines stripped out —
`knowledge_items` and `glossary_terms` themselves are untouched by this,
only their search index:

```bash
cp -r "${STATE_DIR}/system.duckdb.rolling-snapshot" /tmp/snapshot-no-fts
sed -i '/fts_main_/d' /tmp/snapshot-no-fts/schema.sql /tmp/snapshot-no-fts/load.sql
# then IMPORT DATABASE '/tmp/snapshot-no-fts' in place of the recipe above
```

Either way — full import or the stripped-copy workaround — both indexes
rebuild themselves for free the next time the app starts against the
restored file (`app/main.py`'s boot-time `ensure_knowledge_fts_index` /
`ensure_glossary_fts_index` call) or the next time either table is written
through its repository. Until a rebuild completes, `/api/memory?search=` and
the glossary search box serve unranked `ILIKE` results — the same fallback
the whole feature already uses whenever the `fts` extension can't load —
never an error.

Then start the app and verify (see §6).

#### Option B — force the pre-migrate snapshot (data loss: rows since last migration)

Only use this if the broken DB itself is unreadable **and** no usable rolling
snapshot exists (Option A2). The pre-migrate snapshot predates whatever
migration step triggered the WAL corruption. Any data written after the
snapshot was captured is lost.

```bash
# Confirm snapshot version before accepting the loss
docker run --rm -v "${STATE_DIR}:/state:ro" \
  --entrypoint /usr/local/bin/python3 \
  $(docker inspect agnes-app-1 --format '{{.Config.Image}}') \
  -c "
import duckdb
conn = duckdb.connect('/state/system.duckdb.pre-migrate', read_only=True)
print('snapshot version:', conn.execute('SELECT MAX(version) FROM schema_version').fetchone()[0])
print('snapshot applied_at:', conn.execute('SELECT MAX(applied_at) FROM schema_version').fetchone()[0])
"

# If version and applied_at are acceptable, copy the snapshot into place
cp "${STATE_DIR}/system.duckdb.pre-migrate" "${STATE_DIR}/system.duckdb"
```

The app will re-run the migration ladder on the next start (idempotent) and
land at `SCHEMA_VERSION=123`.

#### Option C — restore from a VM or volume snapshot

If you have a disk/volume snapshot taken before the incident, restore the
`<STATE_DIR>` volume from it and restart the app. The app runs the migration
ladder on the restored state.

---

## 5. Checking the discarded WAL (forensics)

The discarded WAL is at `<STATE_DIR>/system.duckdb.wal.discarded.<unix-ts>`
(chmod `0o600`). It holds the raw uncommitted DuckDB WAL bytes — typically
the result of a container kill mid-`ALTER TABLE`. Its content is what DuckDB
failed to replay. It is preserved for forensics and can be removed once you
have confirmed the recovery is complete.

```bash
ls -lh ${STATE_DIR}/system.duckdb.wal.discarded.*
# size is usually small (< 1 MiB for an ALTER-only WAL)
```

DuckDB does not provide a public tool to decode the WAL binary format. The
file is safe to delete once the incident is resolved.

---

## 6. Verification

Run these checks after any recovery option. All commands execute inside the
running app container after restart.

### Schema version

```bash
docker exec agnes-app-1 /usr/local/bin/python3 -c \
  "from src.db import get_system_db, get_schema_version, SCHEMA_VERSION; \
   conn = get_system_db(); v = get_schema_version(conn); \
   print(f'schema_version={v}, expected={SCHEMA_VERSION}, ok={v==SCHEMA_VERSION}')"
```

Expected: `ok=True`.

### Row-count sanity

```bash
docker exec agnes-app-1 /usr/local/bin/python3 -c "
from src.db import get_system_db
conn = get_system_db()
for t in ['users','table_registry','sync_state','knowledge_items',
          'user_groups','resource_grants','marketplace_registry']:
    n = conn.execute(f'SELECT count(*) FROM {t}').fetchone()[0]
    print(f'{t}: {n}')
"
```

Compare against the last known row counts (check the admin dashboard or
previous monitoring snapshots). Zero rows in `users` or `user_groups` after
a Step B restore signals data loss from the snapshot gap — proceed with
Option A or C in §4.

### FK consistency

```bash
docker exec agnes-app-1 /usr/local/bin/python3 -c "
from src.db import get_system_db
conn = get_system_db()
# user_group_members.group_id must reference user_groups.id
orphan_members = conn.execute(
  'SELECT count(*) FROM user_group_members m '
  'WHERE NOT EXISTS (SELECT 1 FROM user_groups g WHERE g.id = m.group_id)'
).fetchone()[0]
# resource_grants.group_id must reference user_groups.id
orphan_grants = conn.execute(
  'SELECT count(*) FROM resource_grants r '
  'WHERE NOT EXISTS (SELECT 1 FROM user_groups g WHERE g.id = r.group_id)'
).fetchone()[0]
print(f'orphan_members={orphan_members}, orphan_grants={orphan_grants}')
print('ok' if orphan_members == 0 and orphan_grants == 0 else 'MISMATCH — investigate')
"
```

### Health endpoint

```bash
curl -sf http://localhost:5000/api/health | python3 -m json.tool
# Expected: {"status": "ok", "db_schema": "ok", "current": 123, "expected": 123, ...}
# (db_schema is a status string — "ok" / "mismatch" / "unreachable";
#  the numeric schema version is in "current".)
```

---

## 7. Cross-references

| Symbol / path | Location |
|---|---|
| `_try_open_system_db` | `src/db.py` — orchestrates Steps A and B |
| `_salvage_discard_wal` | `src/db.py` — Step A: discards WAL, reopens at checkpoint |
| `_move_to_broken` | `src/db.py` — moves broken DB + WAL to `.broken.<ts>` |
| `_peek_schema_version` | `src/db.py` — read-only version probe for the snapshot |
| `_ensure_schema` | `src/db.py` — takes pre-migrate snapshot; runs post-migration `CHECKPOINT` |
| `refresh_rolling_snapshot` | `src/db.py` — Option A2: rolling `system.duckdb.rolling-snapshot/` refresh (#380) |
| `ensure_knowledge_fts_index` / `ensure_glossary_fts_index` | `src/fts.py` — build the `fts_main_*` BM25 index schemas; boot-time rebuild in `app/main.py` (Option A2 search-index note) |
| `SCHEMA_VERSION` | `src/db.py` line 51 — current target version |
| `schema_version` table | `src/db.py` `_SYSTEM_SCHEMA` — `version INTEGER`, `applied_at TIMESTAMP` |
| WAL-recovery tests | `tests/test_db_wal_recovery.py` |
| Rolling-snapshot tests | `tests/test_rolling_snapshot.py` |
| State directory layout | `docs/state-dir.md` — A (nested) vs B (flat) mount topologies |
| Issue #379 | schema-version-aware refusal guard |
| Issue #380 | rolling `system.duckdb.rolling-snapshot/` refresh |
