### Fixed
- **A document with an embedded picture no longer answers as if the picture
  were readable prose.** Converting a `.docx`/`.pptx` (SharePoint crawl or a
  Collections upload) used to leave markitdown's own picture placeholders in
  the indexed text verbatim: a Word image came through as a literal
  `![](data:image/png;base64...)`, and a PowerPoint image as `![](Picture3.jpg)`
  — a name that repeats across slides and, worse, across completely
  unrelated documents, reading like a stable, fetchable filename when it is
  neither. Both are now rewritten into an honest, per-document disclosure —
  `[image 2 of 12 in this document — not indexed, slide 4]` (or `in section
  "…"` for a Word document) — so a reader, or an agent answering from the
  text, is told a passage's real content sits in a picture rather than
  silently reconstructing an answer from surrounding prose. The indexed
  file's own status detail also records the total image count. No OCR or
  image understanding is added — this only makes the loss visible and
  locatable.
