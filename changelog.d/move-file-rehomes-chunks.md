### Fixed
- **Moving a file between collections now carries its search chunks along.**
  Body search scopes on the chunk row's collection, not the file's, so a moved
  file kept answering under the collection it had just left — readable to
  readers granted only that collection and missing from the target's results.
  `POST /api/collections/{id}/files/{file_id}/move` re-homes the file's
  `corpus_chunks` rows right after the file row moves, on both app-state
  backends (`reassign_file_corpus` on the corpus-chunks repository pair).
