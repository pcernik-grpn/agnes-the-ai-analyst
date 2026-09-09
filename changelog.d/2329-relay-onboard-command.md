### Fixed
- **Step 2 of the install prompt now also relays its exact command when the
  agent isn't the one running it**, matching step 1 (#2380). Observed live:
  an agent reasonably declined step 1 (downloading/executing a wheel),
  relayed those commands, but at step 2 (`agnes onboard`) instead tried a
  handful of read-only CLI probes to answer an unrelated question — every
  one got blocked by its own tool-permission classifier, leaving the person
  with no command to run themselves at all.
