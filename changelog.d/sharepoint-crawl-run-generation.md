### Fixed

- SharePoint extraction: a crawl handler that outlives its own force-cancelled
  job can no longer keep mutating that connection's state. Every trigger now
  claims a monotonic per-connection **run generation** (an atomic
  compare-and-set on the connection row, so two triggers can never share one),
  and the inline crawl, the shard planner and every shard child re-check it at
  their existing quiescent points — stopping with a clean, resumable
  `interrupted`/`stopped` outcome instead of walking files, writing crawl state
  and enqueueing children on behalf of a run that is already closed. Unlike the
  cooperative stop flag, which a legitimate retrigger deliberately clears, the
  generation cannot be cleared out from under a zombie. A trigger whose claim
  cannot be recorded (a transient storage failure) still crawls, on the
  cooperative stop flag alone, and stays unclaimed for its whole run — it never
  re-claims later and so can never take ownership away from a run that started
  in the meantime.
- SharePoint extraction: a shard child's job-queue idempotency key is now
  scoped to the run generation that planned it. Two plans' children can no
  longer collide on one key, which previously let a superseded planner's Nth
  child silently dedup onto a fresh run's job row and be accounted to the wrong
  parent run.
- SharePoint extraction: a superseded shard child still advances its parent's
  shard tally — so a parent left behind by a cancelled run cannot wait forever
  on a child that will never report — but the parent is now closed as
  `interrupted`/`stopped` instead of finalized as a completed site run. An
  all-superseded parent previously reached its tally with no child results at
  all and then wrote that empty report over the CURRENT run's connection-level
  crawl state, cleared its cursor seed and could launch a facts-extraction
  pass.
- SharePoint extraction: the shard planner now honors a stop (and a supersede)
  at its own progress checkpoint. A stop pressed during a large site's
  minutes-long shard planning previously went unnoticed until the whole plan
  had been built and enqueued.
- SharePoint extraction: whether a stop request belongs to the run that is
  starting is now decided by the id of the job it was aimed at, not by
  comparing its timestamp against the triggering job's `created_at`. In a
  role-split deployment those two values are written on different hosts, so
  clock skew could make a genuinely fresh Stop request look older than the
  trigger it was meant to cancel and get silently cleared.
- SharePoint extraction: clearing a stop request now clears the id of the job
  it was aimed at along with it, on both app-state backends, so a connection's
  persisted extraction state no longer names a stop target for a stop nobody
  requested.
