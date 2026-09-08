### Internal
- The Devin approval bridge's job token gains `contents: read`: the compare call that decides whether a PR head is ahead of the commit Devin reviewed answered 403 without it, so the resolved-threads rule never fired in production (#2295).
