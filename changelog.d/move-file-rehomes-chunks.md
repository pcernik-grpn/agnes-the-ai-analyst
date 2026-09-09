### Fixed
- **Moving a file between collections now carries its search chunks along.**
  Body search scopes on the chunk row's collection, not the file's, so a moved
  file kept answering under the collection it had just left — readable to
  readers granted only that collection and missing from the target's results.
  `POST /api/collections/{id}/files/{file_id}/move` re-homes the file's
  `corpus_chunks` rows before the file row itself moves, on both app-state
  backends (`reassign_file_corpus` on the corpus-chunks repository pair). The
  order is what makes a half-completed move safe: a failure stops it before
  the file row moves, leaving file and content together in the source, and the
  request replays cleanly.
- **An ingest running while its file is moved no longer writes the chunks into
  the old collection.** Ingestion read the file's collection once at the start
  and wrote chunks under that value minutes later, after conversion and
  embedding — a move landing in that window could not touch rows that did not
  exist yet, so the finished ingest silently restored the leak above. The
  collection is now read at chunk-write time.
