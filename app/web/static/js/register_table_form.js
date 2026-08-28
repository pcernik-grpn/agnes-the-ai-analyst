/* =====================================================================
 * register_table_form.js — the ONE table-registration form (D4 "one
 * registration flow"). Replaces four near-duplicate connector modals
 * (#registerBqModal / #registerKeboolaModal / #registerDatabricksModal /
 * #registerSnowflakeModal, formerly in admin_tables.html) and the
 * onboarding wizard's silent bulk auto-register, with one drawer
 * (`_register_table_form.html`) driven by a per-connector config below.
 *
 * Every registration — from either entry point — POSTs through the same
 * validated `POST /api/admin/register-table` (never a bypass), one row
 * at a time, so a bad row surfaces its own 422/409 instead of aborting
 * (or silently skipping) the rest of the batch.
 *
 * Usage:
 *   RegisterTableForm.open({
 *     sourceType: 'keboola',            // fixes the connector (required)
 *     authHeaders: {Authorization: …},  // optional — bearer-token callers
 *                                       // (the onboarding wizard, pre-session)
 *     onDone: function(summary) {…},    // {registered, failed, total}
 *   });
 *
 * Design notes:
 *   - Every field is rendered ONCE in the shared partial and relabeled /
 *     shown / hidden here per connector + access mode — no per-connector
 *     markup clone.
 *   - Bucket/source_table are never edited in the "Configure" step: every
 *     checked row already carries its own (discovered, or typed via
 *     "+ Add" in the Browse step). Configure only holds the settings
 *     applied to every checked row (bulk defaults) — the one per-row
 *     value worth editing inline (the registered *name*) is exposed only
 *     when exactly one row is checked, via the View name field.
 *   - "Custom query" access modes (BigQuery/Databricks/Snowflake SQL, the
 *     Keboola Storage-API filter) only make sense for one physical table
 *     at a time, so they're disabled whenever more than one row is
 *     checked.
 * ===================================================================== */
(function () {
  'use strict';

  // ── Keboola materialized "custom" fix (D4) ───────────────────────────
  // Pre-D4, the Keboola register modal's "Custom SQL" mode sent a raw
  // SELECT statement as `source_query` for a `query_mode='materialized'`
  // row — but a Keboola materialized row is exported through the Storage
  // API, whose filter is a JSON spec (`connectors.keboola.storage_api.
  // ExportFilter`), not SQL; `RegisterTableRequest._check_mode_query_
  // coherence` rejects any source_query starting with SELECT/WITH for a
  // Keboola materialized row outright. So the mode 422'd on every submit
  // (see the removed TODO(keboola-custom-mode) in admin_tables.html).
  // The fix: this mode reuses the SAME structured where_filters builder
  // as Direct-extract (#408), and wraps its output as the JSON object
  // `ExportFilter.from_dict` expects — `{"where_filters": [...]}"` — not
  // a SQL string. An empty filter list means "no filter", which the
  // registry stores as a NULL source_query (full-table export).

  var CONNECTORS = {
    keboola: {
      label: 'Keboola',
      browseMode: 'full', // discover() returns the whole bucket-grouped catalog
      hasConnectionPicker: true,
      bucketLabel: 'Bucket',
      tableLabel: 'Source table',
      modes: [
        { value: 'whole', title: 'Whole table (extension)', desc: 'DuckDB Keboola extension pulls the full table each tick.' },
        { value: 'direct', title: 'Direct extract (Storage API)', desc: 'Supports incremental, partitioned, where_filters.' },
        { value: 'custom', title: 'Filtered export (Storage API)', desc: 'Optional server-side row filter before the export.' },
        { value: 'remote', title: 'Live (remote)', desc: 'Every query goes straight to Keboola. Nothing syncs.' },
      ],
      defaultMode: 'whole',
      modeShape: function (mode) {
        return {
          queryMode: mode === 'remote' ? 'remote' : (mode === 'direct' ? 'local' : 'materialized'),
          customQuery: mode === 'custom' ? 'kb_filter' : false,
          schedule: mode !== 'remote',
          serverOnly: mode !== 'remote',
          strategy: mode === 'direct',
          advanced: true,
        };
      },
      async discover(ctx) {
        if (ctx.connectionId) {
          var data = await _apiGet('/api/admin/source-connections/' + encodeURIComponent(ctx.connectionId) + '/tables');
          return (data.buckets || []).map(function (b) {
            return {
              key: b.id,
              label: b.name || b.id,
              tables: (b.tables || []).map(function (t) {
                // Keboola's `t.id` is the FULL table id (`in.c-main.orders`);
                // `t.name` is the bare in-bucket name (`orders`). The registry
                // contract keeps the bucket and the bare name in separate
                // columns and composes `kbc.<bucket>.<source_table>` at export,
                // so `source_table` must be the bare one — passing the full id
                // is the #755-era wizard bug that `storage_api.normalize_source_
                // table` exists to heal at use. Healing does not reach the view
                // NAME, though: sanitizing the full id yields `in_c_main_orders`
                // as the analyst-visible name. Fall back to stripping the
                // bucket prefix off `t.id` when `name` is absent — table names
                // cannot contain dots, so the split is unambiguous.
                var bare = t.name || String(t.id || '').slice(String(b.id || '').length + 1) || t.id;
                return {
                  key: b.id + '.' + bare, name: _sanitizeName(bare),
                  bucket: b.id, sourceTable: bare,
                  meta: _fmtCount(t.rows),
                };
              }),
            };
          });
        }
        var discovered = await _apiGet('/api/admin/discover-tables');
        var byBucket = {};
        (discovered.tables || []).forEach(function (t) {
          var bid = t.bucket_id || 'unknown';
          if (!byBucket[bid]) byBucket[bid] = { key: bid, label: t.bucket_name || bid, tables: [] };
          byBucket[bid].tables.push({
            key: t.id || (bid + '.' + t.name), name: _sanitizeName(t.name || t.id),
            bucket: bid, sourceTable: t.name || '',
            meta: _fmtCount(t.row_count),
          });
        });
        return Object.keys(byBucket).map(function (k) { return byBucket[k]; });
      },
      buildPayload(row, mode, s) {
        var base = {
          name: row.name, source_type: 'keboola',
          primary_key: s.primaryKey, connection_id: s.connectionId,
          description: s.description, folder: s.folder,
        };
        if (mode === 'remote') {
          return Object.assign(base, { query_mode: 'remote', bucket: row.bucket, source_table: row.sourceTable });
        }
        if (mode === 'custom') {
          // Unlike BigQuery/Databricks/Snowflake's "custom SQL" (a full
          // SELECT that names its own table), a Keboola materialized
          // source_query is only a JSON *filter* — connectors.keboola.
          // extractor.materialize_query still needs `bucket`/`source_table`
          // to know which physical table to export.
          return Object.assign(base, {
            query_mode: 'materialized', bucket: row.bucket, source_table: row.sourceTable,
            server_only: s.serverOnly, sync_schedule: s.syncSchedule, source_query: s.kbFilterQuery,
          });
        }
        base.sync_schedule = s.syncSchedule;
        if (mode === 'direct') {
          var p = Object.assign(base, {
            query_mode: 'local', bucket: row.bucket, source_table: row.sourceTable,
            server_only: s.serverOnly, sync_strategy: s.kbStrategy,
          });
          if (s.kbStrategy === 'incremental' || s.kbStrategy === 'partitioned') {
            p.incremental_window_days = s.incrementalWindowDays;
            p.max_history_days = s.maxHistoryDays;
          }
          if (s.kbStrategy === 'partitioned') {
            p.partition_by = s.partitionBy;
            p.partition_granularity = s.partitionGranularity;
            p.initial_load_chunk_days = s.initialLoadChunkDays;
          }
          if (s.kbStrategy !== 'incremental') p.where_filters = s.kbWhereFilters;
          return p;
        }
        // whole
        return Object.assign(base, {
          query_mode: 'materialized', bucket: row.bucket, source_table: row.sourceTable,
          server_only: s.serverOnly,
        });
      },
    },

    bigquery: {
      label: 'BigQuery',
      browseMode: 'location',
      locationLabel: 'Dataset',
      bucketLabel: 'Dataset',
      tableLabel: 'Source table / view',
      modes: [
        { value: 'live', title: 'Live from BigQuery', desc: 'Each query goes straight to BQ. Always current.' },
        { value: 'synced_whole', title: 'Synced — whole table', desc: 'SELECT * on a schedule. No SQL required.' },
        { value: 'synced_custom', title: 'Synced — custom query', desc: 'Filter / aggregate before the sync.' },
      ],
      defaultMode: 'live',
      modeShape: function (mode) {
        return {
          queryMode: mode === 'live' ? 'remote' : 'materialized',
          customQuery: mode === 'synced_custom' ? 'sql' : false,
          schedule: mode !== 'live', serverOnly: mode !== 'live',
          project: mode === 'live',
        };
      },
      async listDatasets() {
        var data = await _apiGet('/api/admin/discover-tables');
        return (data.datasets || []).map(function (d) { return d.dataset_id; });
      },
      async discover(ctx) {
        if (!ctx.location) return [];
        var data = await _apiGet('/api/admin/discover-tables?dataset=' + encodeURIComponent(ctx.location));
        return [{
          key: ctx.location, label: ctx.location,
          tables: (data.tables || []).map(function (t) {
            return {
              key: ctx.location + '.' + t.table_id, name: _sanitizeName(t.table_id),
              bucket: ctx.location, sourceTable: t.table_id, meta: t.table_type || '',
            };
          }),
        }];
      },
      buildPayload(row, mode, s) {
        var base = {
          name: row.name, source_type: 'bigquery', profile_after_sync: false,
          description: s.description, folder: s.folder, sync_schedule: s.syncSchedule,
        };
        if (mode === 'synced_custom') {
          return Object.assign(base, { query_mode: 'materialized', source_query: s.customQuery, server_only: s.serverOnly });
        }
        if (mode === 'synced_whole') {
          return Object.assign(base, {
            query_mode: 'materialized', bucket: row.bucket, source_table: row.sourceTable,
            source_query: 'SELECT * FROM bq."' + row.bucket + '"."' + row.sourceTable + '"',
            server_only: s.serverOnly,
          });
        }
        var bqFqn = (s.project && row.bucket && row.sourceTable) ? (s.project + '.' + row.bucket + '.' + row.sourceTable) : null;
        return Object.assign(base, { query_mode: 'remote', bucket: row.bucket, source_table: row.sourceTable, bq_fqn: bqFqn });
      },
    },

    databricks: {
      label: 'Databricks',
      // No catalog-browse endpoint exists yet for Databricks (tracked as a
      // follow-up — see the D4 PR description). Tables are added by name in
      // the Browse step; from there they ride the exact same multi-select →
      // configure → bulk-register pipeline as the other three connectors.
      browseMode: 'manual',
      bucketLabel: 'Schema / bucket',
      tableLabel: 'Source table',
      modes: [
        { value: 'live', title: 'Live from Databricks', desc: 'Each query goes to the SQL warehouse.' },
        { value: 'synced_whole', title: 'Synced — whole table', desc: 'SELECT * on a schedule.' },
        { value: 'synced_custom', title: 'Synced — custom query', desc: 'Filter / aggregate before the sync.' },
      ],
      defaultMode: 'synced_whole',
      modeShape: function (mode) {
        return {
          queryMode: mode === 'live' ? 'remote' : 'materialized',
          customQuery: mode === 'synced_custom' ? 'sql' : false,
          schedule: mode !== 'live', serverOnly: mode !== 'live',
        };
      },
      buildPayload(row, mode, s) {
        var base = {
          name: row.name, source_type: 'databricks', profile_after_sync: false,
          description: s.description, folder: s.folder, sync_schedule: s.syncSchedule,
        };
        if (mode === 'synced_custom') {
          return Object.assign(base, { query_mode: 'materialized', source_query: s.customQuery, server_only: s.serverOnly });
        }
        if (mode === 'synced_whole') {
          return Object.assign(base, { query_mode: 'materialized', bucket: row.bucket, source_table: row.sourceTable, server_only: s.serverOnly });
        }
        return Object.assign(base, { query_mode: 'remote', bucket: row.bucket, source_table: row.sourceTable, server_only: false });
      },
    },

    snowflake: {
      label: 'Snowflake',
      browseMode: 'full',
      bucketLabel: 'Schema',
      tableLabel: 'Source table / view',
      modes: [
        { value: 'live', title: 'Live from Snowflake', desc: 'Each query runs through the DuckDB Snowflake extension.' },
        { value: 'synced_whole', title: 'Synced — whole table', desc: 'SELECT * on a schedule.' },
        { value: 'synced_custom', title: 'Synced — custom query', desc: 'Filter / aggregate before the sync.' },
      ],
      defaultMode: 'live',
      modeShape: function (mode) {
        return {
          queryMode: mode === 'live' ? 'remote' : 'materialized',
          customQuery: mode === 'synced_custom' ? 'sql' : false,
          schedule: mode !== 'live', serverOnly: mode !== 'live',
        };
      },
      async discover() {
        var data = await _apiGet('/api/admin/data-sources/snowflake/tables');
        return (data.schemas || []).map(function (sc) {
          return {
            key: sc.name, label: sc.name,
            tables: (sc.tables || []).map(function (t) {
              return {
                key: sc.name + '.' + t.name, name: _sanitizeName(t.name),
                bucket: sc.name, sourceTable: t.name, meta: t.table_type || '',
              };
            }),
          };
        });
      },
      buildPayload(row, mode, s) {
        var base = {
          name: row.name, source_type: 'snowflake', profile_after_sync: false,
          description: s.description, folder: s.folder, sync_schedule: s.syncSchedule,
        };
        if (mode === 'synced_custom') {
          return Object.assign(base, { query_mode: 'materialized', source_query: s.customQuery, server_only: s.serverOnly });
        }
        var esc = function (v) { return String(v).replace(/"/g, '""'); };
        if (mode === 'synced_whole') {
          return Object.assign(base, {
            query_mode: 'materialized', bucket: row.bucket, source_table: row.sourceTable,
            source_query: 'SELECT * FROM sf."' + esc(row.bucket) + '"."' + esc(row.sourceTable) + '"',
            server_only: s.serverOnly,
          });
        }
        return Object.assign(base, { query_mode: 'remote', bucket: row.bucket, source_table: row.sourceTable });
      },
    },
  };

  // ── generic helpers ──────────────────────────────────────────────────

  function _sanitizeName(raw) {
    var s = String(raw || '').trim().toLowerCase().replace(/[^a-z0-9_]+/g, '_').replace(/^_+|_+$/g, '');
    if (!s) s = 'table';
    if (/^[0-9]/.test(s)) s = 't_' + s;
    return s;
  }

  function _fmtCount(n) {
    if (n == null) return '';
    var num = Number(n);
    if (!isFinite(num)) return '';
    return num.toLocaleString() + ' rows';
  }

  function _errText(detail, fallback) {
    if (typeof window.apiDetailText === 'function') return window.apiDetailText(detail, fallback);
    return (detail && (detail.message || detail.error)) || fallback;
  }

  var state = null; // reset on every open()

  function _authHeaders() {
    return (state && state.authHeaders) || {};
  }

  async function _apiGet(url) {
    var r = await fetch(url, { credentials: 'include', headers: _authHeaders() });
    var data = await r.json().catch(function () { return {}; });
    if (!r.ok) throw new Error(_errText(data && data.detail, 'Request failed'));
    return data;
  }

  async function _apiPost(url, body) {
    var r = await fetch(url, {
      method: 'POST', credentials: 'include',
      headers: Object.assign({ 'Content-Type': 'application/json' }, _authHeaders()),
      body: JSON.stringify(body),
    });
    var data = await r.json().catch(function () { return {}; });
    return { ok: r.ok, status: r.status, data: data };
  }

  function _toast(msg, kind) {
    if (typeof window.showToast === 'function') window.showToast(msg, kind);
  }

  // One connectedness signal (mirrors the pre-D4 per-modal banners, now
  // generic across all four connectors instead of just Keboola/Databricks):
  // `document.body.dataset.connectedSources` — set on /admin/tables to the
  // registry ∪ legacy-scalar ∪ instance-credential-probe union
  // (`connected_sources` in app/web/router.py), ABSENT on setup.html (the
  // onboarding connector is whatever step 2 just configured — there's no
  // "is it connected" question left to ask). `undefined` → skip the check.
  function _applyConnectivityWarning(sourceType) {
    var el = document.getElementById('rtfConnectivityWarning');
    var raw = document.body.dataset.connectedSources;
    if (raw === undefined) { el.hidden = true; return; }
    var connected = raw.split(',').map(function (s) { return s.trim(); }).filter(Boolean);
    if (connected.indexOf(sourceType) !== -1) { el.hidden = true; return; }
    el.textContent = CONNECTORS[sourceType].label + ' is not connected — Browse won\'t find anything yet. '
      + 'Connect it from Data sources, or add a table by name below.';
    el.hidden = false;
  }

  // ── open / close / steps ─────────────────────────────────────────────

  function open(opts) {
    opts = opts || {};
    var sourceType = opts.sourceType;
    if (!CONNECTORS[sourceType]) throw new Error('RegisterTableForm.open: unknown sourceType ' + sourceType);
    state = {
      sourceType: sourceType,
      connector: CONNECTORS[sourceType],
      authHeaders: opts.authHeaders || {},
      onDone: opts.onDone || function () {},
      connectionId: null,
      location: '',
      groups: [],
      manualRows: [],
      selectedKeys: {},
      rowByKey: {},
      mode: CONNECTORS[sourceType].defaultMode,
    };

    var modal = document.getElementById('registerTableModal');
    document.getElementById('rtfTitle').textContent = 'Register ' + state.connector.label + ' tables';
    document.getElementById('rtfSub').textContent = 'Browse ' + state.connector.label + ', pick tables, then register.';
    document.getElementById('rtfSearch').value = '';
    document.getElementById('rtfSelectAll').checked = false;
    document.getElementById('rtfManualEntry').value = '';
    document.getElementById('rtfResults').hidden = true;
    document.getElementById('rtfResults').innerHTML = '';
    document.getElementById('rtfBrowseError').hidden = true;
    document.getElementById('rtfLocationRow').hidden = state.connector.browseMode !== 'location';
    document.getElementById('rtfManualAdd').hidden = false;
    document.getElementById('rtfConnectionPickerField').hidden = !state.connector.hasConnectionPicker;
    _resetConfigureStep();
    _applyConnectivityWarning(sourceType);

    if (state.connector.browseMode === 'location') {
      document.getElementById('rtfLocationLabel').textContent = state.connector.locationLabel || 'Location';
      document.getElementById('rtfBrowseEmpty').textContent = 'Type a ' + (state.connector.locationLabel || 'dataset') + ' above and click "List tables".';
      _prefillLocationList();
    }
    if (state.connector.browseMode === 'manual') {
      document.getElementById('rtfBrowseEmpty').textContent = 'No live browse for ' + state.connector.label + ' yet — add tables by name below.';
    }

    _clearTableList();

    if (state.connector.hasConnectionPicker) {
      _populateConnectionPicker();
    } else if (state.connector.browseMode === 'full') {
      _reloadDiscovery();
    }

    goToStep('browse');
    modal.classList.add('is-open');
  }

  /** Clear every Configure-step control back to its blank/default state.
   *
   *  `close()` only drops the `is-open` class — the drawer's DOM (and so
   *  every value typed into it) outlives the modal. Without this, registering
   *  table B after table A opens on A's description, folder, schedule,
   *  project, SQL, primary key, server-only checkbox and Keboola filter, and
   *  `submit()` forwards them as if the operator had entered them.
   *
   *  Belongs on `open()` rather than `close()`: a reset on close depends on
   *  the drawer having been closed through this function, and the browser's
   *  own bfcache/restore can repopulate fields afterwards. Opening is the one
   *  moment a clean form is actually required. */
  function _resetConfigureStep() {
    ['rtfViewName', 'rtfDescription', 'rtfFolder', 'rtfSyncSchedule', 'rtfProject',
     'rtfCustomQuery', 'rtfPrimaryKey', 'rtfKbPartitionBy', 'rtfKbIncrementalWindowDays',
     'rtfKbMaxHistoryDays', 'rtfKbInitialLoadChunkDays'].forEach(function (id) {
      var el = document.getElementById(id);
      if (el) el.value = '';
    });
    var serverOnly = document.getElementById('rtfServerOnly');
    if (serverOnly) serverOnly.checked = false;
    ['rtfKbStrategy', 'rtfKbPartitionGranularity'].forEach(function (id) {
      var el = document.getElementById(id);
      if (el) el.selectedIndex = 0;
    });
    // The where-filters textarea is the builder's own source of truth: it
    // re-hydrates from this value on the next attach, so leaving the previous
    // table's JSON here would rebuild the previous table's filter rows.
    var wf = document.getElementById('rtfKbWhereFilters');
    if (wf) { wf.value = ''; wf.style.display = 'none'; }
  }

  function close() {
    var modal = document.getElementById('registerTableModal');
    if (modal) modal.classList.remove('is-open');
    state = null;
  }

  function goToStep(step) {
    if (!state) return;
    var isBrowse = step === 'browse';
    document.getElementById('rtfPaneBrowse').classList.toggle('is-on', isBrowse);
    document.getElementById('rtfPaneConfigure').classList.toggle('is-on', !isBrowse);
    var browseBtn = document.getElementById('rtfStepBtnBrowse');
    var configBtn = document.getElementById('rtfStepBtnConfigure');
    browseBtn.classList.toggle('is-now', isBrowse);
    browseBtn.classList.toggle('is-done', !isBrowse);
    configBtn.classList.toggle('is-now', !isBrowse);
    configBtn.disabled = isBrowse && _selectionCount() === 0;
    document.getElementById('rtfBackBtn').hidden = isBrowse;
    document.getElementById('rtfNextBtn').hidden = !isBrowse;
    document.getElementById('rtfSubmitBtn').hidden = isBrowse;

    if (!isBrowse) {
      if (_selectionCount() === 0) {
        goToStep('browse');
        return;
      }
      configBtn.disabled = false;
      _renderConfigureStep();
    }
  }

  // ── Browse step ───────────────────────────────────────────────────────

  async function _populateConnectionPicker() {
    var field = document.getElementById('rtfConnectionPickerField');
    var select = document.getElementById('rtfConnectionPicker');
    select.innerHTML = '';
    var rows = [];
    try {
      var data = await _apiGet('/api/admin/source-connections?source_type=' + encodeURIComponent(state.sourceType));
      rows = Array.isArray(data) ? data : [];
    } catch (e) { rows = []; }
    if (!rows.length) {
      field.hidden = true;
      state.connectionId = null;
      _reloadDiscovery();
      return;
    }
    field.hidden = false;
    rows.forEach(function (c) {
      var o = document.createElement('option');
      o.value = c.id; o.textContent = c.name || c.id;
      select.appendChild(o);
    });
    state.connectionId = select.value || rows[0].id;
    _reloadDiscovery();
  }

  function onConnectionChange() {
    if (!state) return;
    state.connectionId = document.getElementById('rtfConnectionPicker').value || null;
    _reloadDiscovery();
  }

  function onConnectorChange() {
    // Reserved for a future inline connector switch; both current entry
    // points fix sourceType at open() time, so this is a no-op today.
  }

  async function _prefillLocationList() {
    if (typeof state.connector.listDatasets !== 'function') return;
    try {
      var names = await state.connector.listDatasets();
      var dl = document.getElementById('rtfLocationList');
      dl.innerHTML = '';
      names.forEach(function (n) {
        var opt = document.createElement('option');
        opt.value = n;
        dl.appendChild(opt);
      });
    } catch (e) { /* best-effort autocomplete only */ }
  }

  async function loadLocation() {
    var loc = (document.getElementById('rtfLocationInput').value || '').trim();
    if (!loc) { _showBrowseError('Enter a ' + (state.connector.locationLabel || 'location') + ' first.'); return; }
    state.location = loc;
    await _reloadDiscovery();
  }

  async function _reloadDiscovery() {
    if (typeof state.connector.discover !== 'function') { renderTableList(); return; }
    var btn = document.getElementById('rtfLocationLoadBtn');
    if (btn) { btn.disabled = true; btn.textContent = 'Loading…'; }
    _hideBrowseError();
    // The rows about to be replaced are gone from the operator's view, so any
    // checkmark on them is now invisible — and `submit()` reads selectedKeys,
    // not the DOM. Left alone, a table checked under the previous Keboola
    // connection would be registered under the newly selected connection_id,
    // i.e. against the wrong project. Manually-added rows survive: they are
    // not part of the discovered set and the reload does not touch them.
    _dropDiscoveredSelection();
    try {
      state.groups = await state.connector.discover({ connectionId: state.connectionId, location: state.location });
    } catch (e) {
      state.groups = [];
      _showBrowseError(e.message || 'Could not list tables.');
    } finally {
      if (btn) { btn.disabled = false; btn.textContent = 'List tables'; }
    }
    renderTableList();
  }

  function _showBrowseError(msg) {
    var el = document.getElementById('rtfBrowseError');
    el.textContent = msg; el.hidden = false;
  }
  function _hideBrowseError() {
    document.getElementById('rtfBrowseError').hidden = true;
  }

  function _clearTableList() {
    document.getElementById('rtfTableList').innerHTML = '';
    var empty = document.createElement('p');
    empty.className = 'ds-drawer__empty';
    empty.id = 'rtfBrowseEmpty';
    empty.textContent = 'Choose a data source to browse its tables.';
    document.getElementById('rtfTableList').appendChild(empty);
  }

  function _allRows() {
    var rows = [];
    (state.groups || []).forEach(function (g) { rows = rows.concat(g.tables); });
    return rows.concat(state.manualRows);
  }

  var MANUAL_KEY_PREFIX = '__manual.';

  function _isManualKey(key) {
    return String(key).indexOf(MANUAL_KEY_PREFIX) === 0;
  }

  /** Drop every discovered row from the selection, keeping manual ones.
   *  Called when the browsed source changes (connection or location) and the
   *  previous discovered set stops being something the operator can see. */
  function _dropDiscoveredSelection() {
    Object.keys(state.selectedKeys).forEach(function (k) {
      if (!_isManualKey(k)) delete state.selectedKeys[k];
    });
    Object.keys(state.rowByKey).forEach(function (k) {
      if (!_isManualKey(k)) delete state.rowByKey[k];
    });
  }

  /** The one filter predicate — shared by the renderer and by Select all, so
   *  the box can never toggle a row the list is not showing. */
  function _currentSearch() {
    var el = document.getElementById('rtfSearch');
    return ((el && el.value) || '').toLowerCase();
  }

  function _matchesSearch(t, search) {
    return !search
      || t.sourceTable.toLowerCase().indexOf(search) !== -1
      || t.name.toLowerCase().indexOf(search) !== -1;
  }

  function _visibleRows() {
    var search = _currentSearch();
    return _allRows().filter(function (t) { return _matchesSearch(t, search); });
  }

  /** Whether the Select all box should read checked: every row the operator can
   *  currently SEE is selected. Computed over the visible subset, never the
   *  whole discovered catalog — with a filter active those disagree. */
  function _allVisibleSelected() {
    var visible = _visibleRows();
    return visible.length > 0 && visible.every(function (r) { return state.selectedKeys[r.key]; });
  }

  function _selectionCount() {
    return Object.keys(state.selectedKeys).filter(function (k) { return state.selectedKeys[k]; }).length;
  }

  function renderTableList() {
    if (!state) return;
    var container = document.getElementById('rtfTableList');
    container.innerHTML = '';
    var search = _currentSearch();
    var groups = (state.groups || []).slice();
    if (state.manualRows.length) groups = groups.concat([{ key: '__manual', label: 'Added manually', tables: state.manualRows }]);

    var rendered = 0;
    groups.forEach(function (g) {
      var visible = g.tables.filter(function (t) { return _matchesSearch(t, search); });
      if (!visible.length) return;
      var label = document.createElement('div');
      label.className = 'rtf-group-label';
      label.textContent = g.label;
      container.appendChild(label);
      visible.forEach(function (t) {
        state.rowByKey[t.key] = t;
        rendered++;
        var row = document.createElement('label');
        row.className = 'rtf-row';
        var cb = document.createElement('input');
        cb.type = 'checkbox';
        cb.checked = !!state.selectedKeys[t.key];
        cb.addEventListener('change', function () { toggleRow(t.key, cb.checked); });
        var name = document.createElement('span');
        name.className = 'rtf-row-name';
        name.textContent = t.sourceTable;
        var meta = document.createElement('span');
        meta.className = 'rtf-row-meta';
        meta.textContent = t.meta || '';
        row.appendChild(cb); row.appendChild(name); row.appendChild(meta);
        container.appendChild(row);
      });
    });
    if (!rendered) {
      var empty = document.createElement('p');
      empty.className = 'ds-drawer__empty';
      empty.textContent = (state.groups.length || state.manualRows.length) ? 'No tables match your filter.' : 'No tables found yet.';
      container.appendChild(empty);
    }
    document.getElementById('rtfSelectAll').checked = _allVisibleSelected();
    _updateSelectionCount();
  }

  function toggleRow(key, checked) {
    state.selectedKeys[key] = !!checked;
    _updateSelectionCount();
    document.getElementById('rtfSelectAll').checked = _allVisibleSelected();
  }

  function onSelectAllChange() {
    var checked = document.getElementById('rtfSelectAll').checked;
    // Only the rows the operator can currently see. Over `_allRows()` this
    // would register the whole discovered catalog while the list shows a
    // filtered handful — and the hidden ones are never rendered, so nothing
    // in the UI would reveal it before submit.
    _visibleRows().forEach(function (r) { state.selectedKeys[r.key] = checked; });
    renderTableList();
  }

  function addManualRow() {
    var input = document.getElementById('rtfManualEntry');
    var raw = (input.value || '').trim();
    if (!raw) return;
    var parts = raw.split('.');
    var table = parts.pop();
    var bucket = parts.join('.');
    if (!bucket || !table) {
      _showBrowseError('Enter it as "' + (state.connector.bucketLabel || 'bucket') + '.table" — at least one dot.');
      return;
    }
    var key = '__manual.' + raw;
    var row = { key: key, name: _sanitizeName(table), bucket: bucket, sourceTable: table, meta: '' };
    state.manualRows.push(row);
    state.rowByKey[key] = row;
    state.selectedKeys[key] = true;
    input.value = '';
    _hideBrowseError();
    renderTableList();
  }

  function _updateSelectionCount() {
    var n = _selectionCount();
    document.getElementById('rtfSelectionCount').textContent = n ? (n + ' table' + (n === 1 ? '' : 's') + ' selected') : '';
    var configBtn = document.getElementById('rtfStepBtnConfigure');
    if (configBtn) configBtn.disabled = n === 0;
  }

  // ── Configure step ──────────────────────────────────────────────────

  function _selectedRows() {
    return Object.keys(state.selectedKeys)
      .filter(function (k) { return state.selectedKeys[k]; })
      .map(function (k) { return state.rowByKey[k]; })
      .filter(Boolean);
  }

  function _renderConfigureStep() {
    var rows = _selectedRows();
    var n = rows.length;
    document.getElementById('rtfConfigureLede').textContent = n === 1
      ? ('Registering "' + rows[0].sourceTable + '".')
      : ('These settings apply to all ' + n + ' checked tables.');

    _renderModeGroup(n);
    _applyModeVisibility();

    var viewNameField = document.getElementById('rtfViewNameField');
    if (n === 1) {
      viewNameField.hidden = false;
      document.getElementById('rtfViewName').value = rows[0].name;
    } else {
      viewNameField.hidden = true;
    }

    document.getElementById('rtfAdvanced').hidden = state.sourceType !== 'keboola';

    if (state.sourceType === 'keboola' && typeof WhereFiltersBuilder !== 'undefined') {
      if (!state._wfb) {
        state._wfb = WhereFiltersBuilder.attach({
          host: document.getElementById('rtfKbWhereFiltersBuilder'),
          textarea: document.getElementById('rtfKbWhereFilters'),
        });
        document.getElementById('rtfKbFilterRawToggle').onclick = function (ev) {
          ev.preventDefault();
          var ta = document.getElementById('rtfKbWhereFilters');
          var showing = ta.style.display !== 'none';
          if (showing) { ta.style.display = 'none'; state._wfb.rebuild(); }
          else { ta.style.display = ''; ta.focus(); }
        };
      }
    }
  }

  function _renderModeGroup(selectionCount) {
    var host = document.getElementById('rtfModeGroup');
    host.innerHTML = '';
    var modes = state.connector.modes;
    var current = modes.some(function (m) { return m.value === state.mode; }) ? state.mode : state.connector.defaultMode;
    // A "custom query" mode is single-table by nature (one SQL statement /
    // one filter spec) — disable it whenever more than one row is checked,
    // and fall back to the connector's default mode if that was the one
    // already selected.
    var currentShape = state.connector.modeShape(current);
    if (currentShape.customQuery && selectionCount > 1) {
      current = state.connector.defaultMode;
      state.mode = current;
    }
    modes.forEach(function (m) {
      var shape = state.connector.modeShape(m.value);
      var disabled = !!(shape.customQuery && selectionCount > 1);
      var card = document.createElement('label');
      card.className = 'ds-drawer__mode-card';
      if (disabled) card.style.opacity = '0.5';
      var input = document.createElement('input');
      input.type = 'radio'; input.name = 'rtfMode'; input.value = m.value;
      input.checked = m.value === current;
      input.disabled = disabled;
      input.addEventListener('change', function () { state.mode = m.value; _applyModeVisibility(); });
      var title = document.createElement('div');
      title.className = 'ds-drawer__mode-title'; title.textContent = m.title;
      var desc = document.createElement('div');
      desc.className = 'ds-drawer__mode-desc';
      desc.textContent = disabled ? (m.desc + ' (select one table to use this mode)') : m.desc;
      card.appendChild(input); card.appendChild(title); card.appendChild(desc);
      host.appendChild(card);
    });
  }

  function _applyModeVisibility() {
    var shape = state.connector.modeShape(state.mode);
    document.getElementById('rtfProjectField').hidden = !shape.project;
    document.getElementById('rtfScheduleRow').hidden = !shape.schedule;

    var customField = document.getElementById('rtfCustomQueryField');
    var kbFilterField = document.getElementById('rtfKbFilterField');
    customField.hidden = shape.customQuery !== 'sql';
    kbFilterField.hidden = shape.customQuery !== 'kb_filter';
    if (shape.customQuery === 'sql') {
      document.getElementById('rtfCustomQueryLabel').textContent = 'SQL';
      document.getElementById('rtfCustomQueryHint').textContent =
        'SELECT statement, no trailing semicolon. Result is materialized to parquet.';
    }

    document.getElementById('rtfKbStrategyPanel').hidden = !shape.strategy;
    if (shape.strategy) onKbStrategyChange();
  }

  function onKbStrategyChange() {
    var strategy = document.getElementById('rtfKbStrategy').value;
    document.getElementById('rtfKbIncrementalFields').hidden = !(strategy === 'incremental' || strategy === 'partitioned');
    document.getElementById('rtfKbPartitionFields').hidden = strategy !== 'partitioned';
    // Reuse the same where_filters builder markup for Direct-extract's
    // `where_filters` array — only the payload key differs from the
    // 'custom' materialized mode's JSON-wrapped `source_query` (both are
    // built from the same builder state in `_collectSettings`). Not
    // compatible with the Incremental strategy (changedSince already
    // scopes the pull).
    document.getElementById('rtfKbFilterField').hidden = strategy === 'incremental';
  }

  // ── settings collection + submit ─────────────────────────────────────

  function _intOrNull(id) {
    var v = (document.getElementById(id).value || '').trim();
    if (!v) return null;
    var n = parseInt(v, 10);
    return isNaN(n) ? null : n;
  }

  function _collectSettings() {
    var s = {
      description: (document.getElementById('rtfDescription').value || '').trim() || null,
      folder: (document.getElementById('rtfFolder').value || '').trim() || null,
      syncSchedule: (document.getElementById('rtfSyncSchedule').value || '').trim() || null,
      serverOnly: !!document.getElementById('rtfServerOnly').checked,
      project: (document.getElementById('rtfProject').value || '').trim() || null,
      customQuery: (document.getElementById('rtfCustomQuery').value || '').trim(),
      primaryKey: (document.getElementById('rtfPrimaryKey').value || '')
        .split(',').map(function (v) { return v.trim(); }).filter(Boolean),
      connectionId: state.connectionId || null,
      kbStrategy: document.getElementById('rtfKbStrategy').value,
      incrementalWindowDays: _intOrNull('rtfKbIncrementalWindowDays'),
      maxHistoryDays: _intOrNull('rtfKbMaxHistoryDays'),
      partitionBy: (document.getElementById('rtfKbPartitionBy').value || '').trim() || null,
      partitionGranularity: document.getElementById('rtfKbPartitionGranularity').value || null,
      initialLoadChunkDays: _intOrNull('rtfKbInitialLoadChunkDays'),
    };
    if (!s.primaryKey.length) s.primaryKey = null;
    var rawFilters = [];
    try { rawFilters = JSON.parse(document.getElementById('rtfKbWhereFilters').value || '[]'); } catch (e) { rawFilters = []; }
    s.kbWhereFilters = rawFilters.length ? rawFilters : null;
    s.kbFilterQuery = rawFilters.length ? JSON.stringify({ where_filters: rawFilters }) : null;
    return s;
  }

  async function submit() {
    var rows = _selectedRows();
    if (!rows.length) return;
    var shape = state.connector.modeShape(state.mode);
    if (shape.customQuery === 'sql' && !(document.getElementById('rtfCustomQuery').value || '').trim()) {
      _toast('SQL is required for a custom query registration.', 'error');
      return;
    }
    var settings = _collectSettings();
    if (rows.length === 1) {
      var renamed = (document.getElementById('rtfViewName').value || '').trim();
      if (renamed) rows[0] = Object.assign({}, rows[0], { name: renamed });
    }
    if (shape.strategy && settings.kbStrategy === 'partitioned' && !settings.partitionBy) {
      _toast('Partition-by column is required for the Partitioned strategy.', 'error');
      return;
    }

    var btn = document.getElementById('rtfSubmitBtn');
    btn.disabled = true;
    btn.textContent = 'Registering…';
    var resultsEl = document.getElementById('rtfResults');
    resultsEl.hidden = false;
    resultsEl.innerHTML = '';

    var registered = 0, failed = 0;
    for (var i = 0; i < rows.length; i++) {
      var row = rows[i];
      var payload = state.connector.buildPayload(row, state.mode, settings);
      var res = await _apiPost('/api/admin/register-table', payload);
      var line = document.createElement('div');
      if (res.ok) {
        registered++;
        line.className = 'rtf-result-row is-ok';
        line.innerHTML = '<span class="rtf-result-name"></span><span class="rtf-result-msg"></span>';
        line.querySelector('.rtf-result-name').textContent = row.name;
        line.querySelector('.rtf-result-msg').textContent = 'Registered.';
      } else {
        failed++;
        line.className = 'rtf-result-row is-error';
        line.innerHTML = '<span class="rtf-result-name"></span><span class="rtf-result-msg"></span>';
        line.querySelector('.rtf-result-name').textContent = row.name;
        line.querySelector('.rtf-result-msg').textContent = _errText(res.data && res.data.detail, 'Registration failed (HTTP ' + res.status + ')');
      }
      resultsEl.appendChild(line);
    }

    btn.disabled = false;
    btn.textContent = 'Register';

    if (registered) _toast(registered + ' table' + (registered === 1 ? '' : 's') + ' registered.', 'success');
    if (failed) _toast(failed + ' table' + (failed === 1 ? '' : 's') + ' failed — see the list below.', 'error');

    var summary = { registered: registered, failed: failed, total: rows.length };
    if (typeof state.onDone === 'function') state.onDone(summary);
    if (!failed) close();
  }

  // Plain top-level function (not a `RegisterTableForm.*` property) so the
  // app-wide Escape handler's `data-close-handler` lookup (_app_scripts.html
  // — `window[fn]`, string names only) can resolve it. Its generic fallback
  // (`target.style.display = 'none'`) would otherwise leave an inline style
  // that outranks the `.is-open` class rule on the next open() — routing
  // through this shim keeps close() as the one place that tears the drawer
  // down.
  window.closeRegisterTableModal = function () { close(); };

  window.RegisterTableForm = {
    CONNECTORS: CONNECTORS,
    open: open,
    close: close,
    goToStep: goToStep,
    onConnectorChange: onConnectorChange,
    onConnectionChange: onConnectionChange,
    loadLocation: loadLocation,
    renderTableList: renderTableList,
    onSelectAllChange: onSelectAllChange,
    addManualRow: addManualRow,
    onKbStrategyChange: onKbStrategyChange,
    submit: submit,
  };
})();
