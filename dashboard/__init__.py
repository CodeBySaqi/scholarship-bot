"""The control panel: pages, JSON API, config writes, jobs, Telegram pairing.

Layout, and why each piece is separate:

  ui.py         one HTML string, no CDN, relative URLs          (presentation)
  server.py     stdlib HTTP adapter + token gate                (transport)
  api.py        `dispatch(ctx, method, path, query, body)`      (routing, tested)
  query.py      filters and row actions over the DB             (reads)
  jobs.py       one-at-a-time background runs with a log buffer  (side effects)
  settings_io.py  what may be edited, and a validated atomic write
  patch.py      comment-preserving YAML/.env editing           (no yaml.dump)
  telegram.py   getMe / getUpdates / sendMessage

Nothing here reimplements scraping or scoring: a job calls the same
`core.pipeline.run` that `cli.py run` calls, so what the page shows is what the
nightly Action will produce. `server.serve` is the entry point, and
`cli.py dashboard` builds the Ctx and calls it.
"""

from __future__ import annotations
