# Future ideas

These are product ideas for later discussion. They are **not implemented or approved work**.

## 1. Confirm Shazam matches across neighboring chunks

Shazam currently analyzes each captured chunk independently. A returned title and track key are treated as a match, even if adjacent chunks return different songs.

Proposed behavior:

- Preserve every raw Shazam response for inspection.
- Treat individual results as candidates. Confirm a song only when at least three of four time-contiguous chunks agree on the same underlying song and their match offsets are plausible.
- Allow a recorder restart across a consensus window if capture times remain continuous. A real gap or unidentified interval breaks the window.
- Show confirmed songs in Play History. Keep unsupported or conflicting chunks as uncertain and preserve their audio for review.
- Keep automatic deletion stricter: require confirmed matches plus the existing exact-track and offset checks.

This would delay confirmation by roughly 45–60 seconds and may leave short songs or transitions uncertain. Repeated false matches can still pass consensus, so raw evidence should remain available.

## 2. Normalize remix/version variation for display

Shazam may alternate between catalog entries such as a base song and a remix of that song for adjacent chunks.

Proposed behavior:

- Keep each chunk's original Shazam title, track key, and response unchanged.
- Derive a separate comparison key from artist and title, stripping only recognized version suffixes such as an explicitly labeled remix.
- Group matching variants as one underlying song for consensus and play-history display.
- Display the exact version if one variant clearly wins. If results are split, show the base title with a “version uncertain” indicator and retain the observed variants in Details.
- Never use this relaxed display grouping to authorize audio deletion.

This improves consistency without claiming to know which version is playing when Shazam disagrees.

## 3. Browse confirmed history by day and export it

The recording database lives on the user's Unraid server. A GitHub Pages site cannot read it directly. Publishing live history there would require exposing the Unraid server or deliberately copying history to GitHub.

For an MVP, keep history local to Airlog and consider adding:

- A calendar-day selector in the existing Play History view.
- A per-day CSV export of confirmed song plays, with one row per play rather than one row per captured chunk.
- Day boundaries based on a clearly defined timezone, preferably the user's configured station timezone.

The current CSV export contains chunk-level recognition observations, not grouped confirmed plays. A future public GitHub Pages site could be added as a separate opt-in publishing feature, using sanitized daily JSON or CSV snapshots. That would make the published history public and should require an explicit decision.
