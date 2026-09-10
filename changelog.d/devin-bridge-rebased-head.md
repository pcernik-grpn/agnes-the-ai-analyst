### Internal
- The Devin-clean approval bridge (`devin-clean-approves.yml`) no longer strands
  a rebased PR: when Devin's last verdict listed issues, every thread is
  resolved, and the head is "diverged" from the reviewed commit instead of
  "ahead", it accepts Devin's own analysis of the current head (a "Devin
  Review" success status with no issue review posted at that commit) as the
  witness that the findings are addressed.
