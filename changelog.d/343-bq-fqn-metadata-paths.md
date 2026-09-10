### Fixed
- **A BigQuery table registered in a project other than the configured one now
  reports its real schema, sample and catalog metadata.** `bq_fqn` pins a row's
  own `project.dataset.table`, but only the execution paths consulted it, so
  `agnes schema`, `agnes describe` / `agnes sample`, and the catalog's
  entity/row-count refresh still addressed
  `<configured-project>.<bucket>.<source_table>`. What sat at that address
  decided the symptom: an upstream error when the dataset was absent, an empty
  column list when only the table was, and — silently — **another object's
  schema** when a same-named one existed there. `/api/v2/scan` inherited the
  last case through its column validator, rejecting real columns as
  `unknown columns: [...]` on a table its own SQL builder addressed correctly.
  Rows without `bq_fqn` are unaffected.
