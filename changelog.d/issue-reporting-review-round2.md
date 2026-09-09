### Fixed
- **Reporting under load no longer stalls other requests.** The wait for a
  screenshot ran on a request worker thread, so enough simultaneous reports
  could exhaust the pool and block authentication — including for the very
  upload each wait was waiting for. The wait now costs no thread.
- **`#42` works in the MCP issue tools.** The `#` the tools document started a
  URL fragment, so the server received no issue number at all.
- **An admin opening the issue queue or someone else's report is on the record.**
  Those reads return another person's words and now carry a cataloged audit
  action instead of a UI-support exemption.
- **Replacing a screenshot no longer serves half a file.** Uploads are published
  atomically, and a refused upload leaves nothing behind.
- **`agnes issue report` stopped claiming the operator copy failed.** The
  creation response always predates the background delivery, so the line it
  printed was never anything but a guess.
