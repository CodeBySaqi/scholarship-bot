# Blueprint review — 26 changes I made and why

Ordered by how much they matter. "blueprint" = the design you pasted;
"here" = what's in this repo. File references are real paths.

---

## A. Correctness bugs in the blueprint (these would have bitten you in week 1)

**1. `url = Column(String(1000), unique=True)` + `filter_by(url=...)` → duplicate rows *and* crashes.**
Any trailing slash, `http` vs `https`, `www.`, `index.html` or `?utm_source=…` produced a
"new" row for an existing programme; when two of them normalised to the same string the
UNIQUE constraint threw mid-run and the whole cycle died.
→ `db/models.py`: `normalize_url()` (scheme, host case, `www.`, index page, trailing slash,
tracking params) stored in `normalized_url`; the pipeline upserts on it. Test:
`TestUpsert::test_url_variants_do_not_create_rows`.

**2. `declarative_base()` from `sqlalchemy.ext.declarative` is deprecated** in SQLAlchemy 2.0
and emits a warning that becomes an error in 2.1.
→ `db/models.py` uses `DeclarativeBase` + `Mapped[...]` / `mapped_column`.

**3. LLM JSON was never validated before hitting the DB.**
`json.loads(response…)` then `parsed.get('deadline')` → `datetime.strptime(…, "%Y-%m-%d")`
inside a bare `except:`. A model answering `"12 Jan 2027"` or `"TBD"` silently produced
`deadline = None`, and the row looked "fine".
→ `parsers/schema.py`: one Pydantic model used by *both* the regex path and the LLM path;
bad values are repaired field-by-field or dropped, never guessed. Tests:
`TestSchema::test_feb_31_rejected`, `test_deadline_must_be_a_real_date`.

**4. `requires_ielts = Column(Boolean)` while the eligibility text says "IELTS 6.5".**
A boolean can't be compared to your 7.5, so the blueprint could not tell "you qualify" from
"you don't". Same for `min_gpa` with no scale: a 3.6/5.0 requirement vs a 3.6/4.0 GPA is a
*fail*, and it scored as a pass.
→ `min_ielts`, `min_toefl`, `min_gpa` + `gpa_scale` are numeric and gate-checked
(`matching/matcher.py::check_gates`), scale-aware, and tolerant of prose scales like
`"UK upper second"` (that exact value crashed my first version — now
`_gpa_scale()`).

**5. `from telegram import Bot` + `asyncio.run()` in a synchronous `schedule` loop.**
`python-telegram-bot` v20 is async-only and needs an event loop you weren't running; the
telegram notifier was dead code that imports fine and fails at night.
→ `notifiers/__init__.py` calls the Bot API over plain HTTPS (`api.telegram.org/bot<token>/sendMessage`)
— no 20-MB dependency, no event loop, 4096-char chunking.

**6. `schedule.every().day.at("09:00")` + `while True` in a GitHub Action.**
Actions kill the job after ~6 h (and the nightly schedule would just idle in a loop), and
`send_digest` was called *before* the loop, so a manual dispatch emailed you, then slept.
→ the workflow runs `python cli.py run` once and exits; scheduling is cron's job
(`.github/workflows/nightly-scrape.yml`). The loop is only for local/systemd use.

**7. Notifications keyed on `notified=False` only.**
First run marks 500 old rows notified; then any row that fails the ≥50 filter *forever*
stays `notified=False`, and every night `filter_and_score` re-ranks it. Meanwhile a genuine
change (deadline moved from 1 Dec to 15 Jan) is invisible because the row already exists.
→ `is_new` / `changed` / `change_type` are edge flags, cleared at the end of the cycle
(`upsert_scholarship` resets them on an "unchanged" result), and `deadline_moved` /
`funding_changed` **re-arm** notification on purpose. `notification_log` + a digest hash
stop the same set being sent twice inside `reschedule_window_hours`.

**8. Scrapers that print `[ERROR] … return None` and the run reports success.**
You learn a source died by noticing 3 months of silence.
→ `SourceRun` table records per-source `status/items_found/items_new/error/duration`,
`cli.py health` prints it, the workflow writes a step summary and opens a `scraper-health`
issue, and `core/web.py` has a circuit breaker so one blocked host doesn't stall the rest.

---

## B. Cost and politeness

**9. One 6000-token GPT call per scholarship, every day, forever.**
That's the headline problem. In my live test, 20 listing rows → ~120k input tokens/night
≈ $0.02/night just to re-derive `Deadline: 6 Oct 2026` that was printed on the page.
→ three-layer funnel: (1) HTML cache with conditional GET (`core/web.py`);
(2) `record_hash` over the *parsed facts* skips re-extraction of unchanged rows;
(3) `parsers/fields.py` + `parsers/labels.py` extract deadline/amount/GPA/IELTS/coverage/
degree/country deterministically, and the LLM is called only when ≥2 key fields are missing
(`LLMClient.needs_llm`). **Measured: 16/16 rows at 100% confidence, 0 LLM calls, $0.000.**

**10. No caching at all** — every run re-downloaded everything.
→ `HtmlCache` (SQLite) storing `ETag` / `Last-Modified` and sending `If-None-Match`;
a 304 means ~0 bytes. `ParseCache` does the same for model output keyed by content hash,
so re-runs and resumed runs are free.

**11. Playwright launched per page, unconditionally.**
`with sync_playwright()` inside `fetch_dynamic` costs a browser start per URL and ~400 MB
of CI install time even for static sites.
→ `requests` first; Playwright only when the HTML looks like an empty JS shell
(`_looks_like_empty_shell`) or `render: true` is set in that source's config; the workflow
installs Chromium *conditionally*.

**12. No rate limiting or robots handling.** `random_delay(2, 5)` was defined and never
called in the blueprint's own flow.
→ `DomainThrottle` (per-domain min interval + jitter) is enforced inside `Web.fetch`,
`Robots` (cached, fail-open on network error) is behind `respect_robots`, and 429/5xx get
exponential backoff with full jitter before falling back to stale cache.

**13. Playwright's `page.wait_for_selector` on a 30 s timeout, no UA rotation.**
→ UA rotation on 403, stale-cache serving, `source=`-scoped breaker so a blocked source is
skipped (not retried 8×) while it cools down.

---

## C. Data model & lifecycle

**14. `amount` as a string, `min_gpa` as float, no `open_date`, no intake/round, no
`deadline_estimated`.** You cannot sort, filter or "what changed" against a string column.
→ `amount_text` + `amount_usd` + `currency` (static FX, annualised: monthly ×10),
`application_fee_usd`, `open_date`, `intake`, `round_label`, `deadline_text` (the original
string stays visible), `deadline_rolling`, `deadline_estimated`, `coverage` (structured list
of what's paid for), `country_scope`, `eligible_countries`/`excluded_countries`,
`max_age`, `max_years_since_degree`, `min_work_experience_years`, `requires_work_experience`.

**15. Nothing marked expired, and rows never disappeared.** The blueprint's DB only grows;
`WHERE deadline < now` rows stay `is_new=True` and keep the notification pool noisy.
→ `mark_expired()` sweeps to `status = expired | outdated | stale` (unseen for
`stale_days`), and reports/dashboards default to `active` only. Expired rows are kept for
history, not deleted.

**16. Annual programmes broke the tracker.** Scholars4Dev publishes
`Deadline: 6 Oct 2026 (annual)`; on 20 Oct 2026 that row is dead forever and you never see
the 2027 reopening *under the same URL*.
→ `parsers/labels.py` reads the `(annual)` marker and `rolling` phrases;
`_postprocess()` rolls a past deadline forward on recurring programmes, flags
`deadline_estimated`, and writes a `verify` note. Also: if the page's own
`Last updated:` is *newer* than the deadline it still advertises, the row is treated as
stale and rolled (test: `TestDeadline::test_literal_day_beats_month_end_guess`).

**17. Same programme from 4 sources = 4 emails.** Chevening appears on Scholars4Dev,
Opportunities Corners, the university page and its own site.
→ `canonical_key = sha256(normalised_title | deadline)`: a second source merging into the
same key updates `seen_in_sources`, bumps `times_seen`, appends the URL to
`extras.alt_urls`, and reports outcome `duplicate` (no new row, no new email).

**18. `SELECT *` style queries with no indexes** and JSON stuffed into `Text`.
→ indexed on `status+deadline`, `tier+match_score`, `source`, `match_score`,
`normalized_url`; JSON columns are `JSON` where SQLAlchemy supports it and `Text` + explicit
encode for SQLite compatibility; `_lightweight_migrate()` ALTERs in new columns so you don't
lose history when you upgrade.

**19. Raw HTML stored as `description` (10 kB × thousands of rows).**
→ the stored text is the *cleaned* extraction (`clean_text`, 60 kB cap) and both a
`content_hash` (page bytes, diagnostics) and `raw_hash` (parsed facts, change detection)
are kept instead of the bytes.

---

## D. Configuration & extensibility

**20. A `config.yaml` that the code never reads**, and a hardcoded `PROFILE` dict inside
`filters/matcher.py`.
→ one `config.yaml` for profile / weights / tier cut-offs / notify policy / sources, with
`SCHOLARSHIP__*` env overrides for CI, and secrets only ever from `.env`
(`core/env.py` is a 40-line dotenv loader so `python-dotenv` isn't needed).

**21. One Python file per source with CSS selectors welded in.** A redesign = a code
change = a broken bot for a week.
→ five declarative kinds (`listing`, `table`, `rss`, `json_api`, `seed`) configured in YAML;
selector errors are caught per page and reported instead of crashing the run
(`TestListingScraper::test_bad_selector_reports_error_instead_of_raising`).

**22. Priority-1 source list that doesn't fit the user.** `scholarshipdb.net` (US
domestic), `fulbrightonline.org` and `educanada.ca` are the wrong targets for an applicant
in Punjab; the sources with the best signal-to-noise for Pakistani master's/PhD applicants
are Commonwealth/funder programmes.
→ kept Scholars4Dev (it works and is scraper-friendly), replaced the US-centric ones with a
curated seed set (Chevening, Fulbright Pakistan, DAAD EPOS, EMJM, CSC, Türkiye Bursları,
MEXT, GKS, Stipendium Hungaricum, Commonwealth Shared, SIIS, AKF ISP, Australia Awards,
NAWA, KGEP, OBOR/COMSATS), and left `scholarshipdb`/`daad` in config as `enabled: false`
with a comment saying *why* (they 403 from datacenter IPs — verified during the build).

---

## E. Output & usability

**23. Email only, HTML-string-concatenated, unescaped.** `f"…{s.eligibility[:300]}…"` with
unescaped text from the web is both an email-client XSS vector and a `TypeError` when
`eligibility` is None (very common).
→ `Digest` builds rows once and renders `markdown()` / `plain()` / `html()` with
`html.escape()` everywhere; SendGrid was replaced by plain SMTP (works with Gmail/Mailtrap/
SES/Postmark and costs nothing), plus console/Telegram/webhook (Slack/Discord/generic).
Cap per source (`max_per_source: 3`) and per tier so one aggregator can't flood you.

**24. No way to browse history.** Email is a terrible index.
→ `reporting/__init__.py` writes a self-contained `data/out/index.html` (search, filters,
score-breakdown bars, deadline urgency — all client-side, zero network deps, publishable to
GitHub Pages) + `data.json` + `scholarships.csv` + `summary.md`. `cli.py dashboard` serves it.

**25. No "why did this rank / why was this dropped".**
→ every row keeps `score_breakdown` (per-dimension points, tier, confidence, days_left) and
`gate_reasons` (`reasons` + `warnings`); `cli.py query --explain` prints them and the
dashboard draws them as a stacked bar. Also `quality_flags`
(`deadline_estimated`, `no_amount`, `eligibility_unlisted`, `low_confidence`).

**26. `min_score >= 50` as a magic number, `score += 30` for countries.**
→ weights and tier cut-offs are config, scores are normalised to 0–100 and multiplied by a
confidence factor, and urgency is applied as a bounded multiplier rather than "+5 points" —
so a 90-point match that closes in 5 days can beat a 92-point match closing in 9 months.

---

## Deliberately *not* changed / not done

* **SQLite, not Postgres.** Fine for one user; `DATABASE_URL` is respected so you can point at
  Postgres without code changes. No Alembic — `_lightweight_migrate()` covers the realistic
  "add a column" case, and I'd add Alembic only if you start sharing the DB.
* **No web app / auth / opt-out endpoint.** `notify-preview --html` and the `opt_out` table +
  token in the footer are the plumbing; wiring a real unsubscribe URL needs a tiny HTTP
  endpoint, which is exactly the kind of always-on server I was trying to avoid.
* **`optimise` robots default = `false`.** Deliberate for personal use at home; flip it on if
  you run this from a domain you own.
* **Not verified here (no network/creds in the sandbox):** real SendGrid/Gmail delivery, real
  Telegram send, Playwright rendering (no browser binaries installed), and the
  `api.eacea.ec.europa.eu` scraper (that host doesn't resolve from this sandbox — so it ships
  `enabled: false`, and the JSON-API code path is covered by fixtures instead of by a live call).
* **`langchain` dropped.** It was used for a demo agent only; `agent.py` gives the same
  capability with `urllib` and three real functions, and can be pointed at any
  OpenAI-compatible endpoint — including Ollama, so the whole stack can run at $0.

---

## F. Three more bugs, found by actually running it (later additions)

**27. Same programme in two categories = permanent "changed" churn.**
Scholars4Dev lists Chevening under *Masters*, *PhD* and *Pakistan*. Each source wrote
`source = <its own id>`, so `source` flipped every run and the row looked edited forever
(run 1: masters wins; run 2: phd "changes" it; run 3: masters again…).
→ `source` and `seen_in_sources` are now **outside** the change comparison
(`NON_COMPARABLE`, `db/models.py`), and `seen_in_sources` accumulates every publisher
(`scholars4dev_masters,scholars4dev_phd`) instead of overwriting.

**28. Scoring-owned columns were compared during upsert.**
`confidence` is written as 0.5 when the row is stored and only updated by the scoring pass
that runs *afterwards* — so every multi-source run reported `changed=N` for rows nobody
touched.
→ `match_score / tier / confidence / gate_pass / gate_reasons / score_breakdown /
quality_flags` joined `NOISE_FIELDS`. **Verified: two consecutive full runs over 4 live
sources now report `new=0 changed=0 skipped=57`.**

**29. `profile.work_experience_years: 1.5` in config.yaml was never read.**
`Profile.from_dict` copied an explicit list of keys and `work_experience_years` wasn't in
it → the dataclass default `0.0` won, and *every* programme needing 2 years (Chevening,
DAAD EPOS, Commonwealth…) was hard-gated out with the reason "you have 0". A config file
that lies silently is worse than no config file.
→ `Profile.from_dict` now coerces **any** remaining key from its type annotation
(`get_type_hints` + `fields()`), and warns instead of guessing on unparseable values.
Locked with `tests/test_config.py::test_every_profile_key_in_config_maps_to_a_field`, which
fails if anyone adds a `profile:` key with no matching field.

**30. (Bonus, same class) prose in `eligible_countries` disqualified the best match.**
The seed said `"Chevening-eligible countries (Pakistan is eligible in most cycles)"`; the
gate read a list of one entry that wasn't Pakistan and rejected it. Unparseable /
mostly-prose eligibility lists now return *no gate* (with a warning) — a false "not
eligible" permanently hides a scholarship, a false "maybe" only costs one page visit.

**31. `python -m ruff check .` was run (it hadn't been once) and its autofix broke the build.**
`ruff --fix` rewrote a local `import re as _re` in `scrapers/__init__.py` and left a
`_re.IGNORECASE` reference outside that branch → `UnboundLocalError` in the table scraper.
Caught only because the test suite ran right after; the offending local imports are gone.
Remaining findings are deliberate: `BLE001` ×20 (a raising scraper kills the whole nightly
run, so broad catches with a logged `SourceRun.error` are the design) and `DTZ003` ×10
(`utcnow()` is our single naive-UTC convention, stored as UTC in SQLite).

**32. The CI could report success when the whole pipeline crashed.**
Two independent causes: (a) `python cli.py run --json | tee run.json; echo "exit=$?"`
captured **tee's** exit code, and (b) the `Summarise run` step had `if: always()` but no
`id:`, so `steps.summarise.outputs.*` was empty everywhere — the "fail loudly" step could
never fire. A `run.json` containing a traceback was also unparseable by the summariser.
→ new `ci/wrap_run.py` guarantees `run.json` is always valid JSON (adding `fatal` + the
stderr tail) and `ci/summarise.py` exits 1 on a fatal or an id-less run. Covered by
`tests/test_ci_tools.py`: all-sources-failed → `all_failed=true`, `{}` → exit 1, unset
`$GITHUB_*` (`"None"`) no longer raises, traceback → crash card + exit 1.

**33. `cli.py dashboard` had no `--host`** and printed a URL it hadn't bound to.
→ `--host` flag (default `0.0.0.0`, so it works behind a proxy/tunnel), `allow_reuse_address`.
Verified live: `GET /` → 200, `GET /data.json` → 200, 45 embedded rows, all filter widgets
(`q`, `tier`, `country`, `degree`, `fund`, `sort`) present, zero external `src`/`href`.

*Not everything I suspected was a bug:* I checked whether `dry_run` mode poisoned the
notification dedupe log (it would have blocked the first real send) — `already_sent()`
filters on `status == "sent"`, and `config.yaml` ships `dry_run: false`, so there was
nothing to fix. Left as is.

**34. Two doc promises I could not keep silently, so I checked them instead.**
`tests/test_config.py::test_llm_keys_in_config_are_the_ones_the_client_reads` now asserts
that `parsers/llm.py` reads exactly `model / batch_size / max_item_chars / budget_usd /
timeout / retries` and that `config.yaml` supplies them — a renamed key used to fall back to
the client default with no signal at all (a "typo" that would have quietly batched 5 → 25
items per call). And `test_every_profile_key_in_config_maps_to_a_field` proves no `profile:`
key is unmapped today (it returns `[]`, which is what caught #29). `README.md` / `tests.yml`
referenced `requirements-dev.txt`, and `agent.py` + `.env.example` were referenced but I had
not confirmed them: all three now exist, and the README's `report --format markdown` /
`#f-tier` claims were corrected to the real flags (`--out/--limit/--statuses`) and real
element ids (`q, tier, country, degree, fund, sort`).

---

## G. Source coverage + the delivery path, checked by running them

**35. Probed 60+ candidate sites before adding any, and added only the four that
survive contact.**
Enabled: 3 more Scholars4Dev lists (undergraduate, engineering, Germany) and kept
the seed set. Measured, then rejected: `euraxess` (200 + real `Deadline:` strings
but the rows are *vacancies*, not awards), `studying-in-germany` (200 but one long
prose article — no per-item nodes), `brightscholar` / `edufind` (JS shells),
`istudies` (TLS), `scholarshipportal` / `daad` (403), `scholarshippositions` /
`chevening` / `korea` (timeouts), `hec.gov.pk` / `campuschina` / `findscholarships`
/ `applykwik` (DNS). One shared `Web` object during probing also demonstrated the
circuit breaker opening (599 `circuit open`) instead of hammering the host.

**36. Cross-source overlap was silently costing detail fetches.**
`skip_known` drops a listing row before the pipeline ever sees it, so the row never
learned its second publisher: `seen_in_sources` stayed at one id for all 67 rows
even though 15 URLs are listed by 2–4 of the sources. → `BaseScraper._flush_touches()`
merges publishers and bumps `times_seen` for skipped rows at the end of a scrape;
`upsert_scholarship` and the facts-hash skip path share one `merge_sources()` helper
so all three write paths agree. After: `rows credited to 2+ sources: 15`, one
programme showing `scholars4dev_engineering,scholars4dev_masters,scholars4dev_pakistan,scholars4dev_phd`.

**37. The email/telegram/webhook senders had never actually run. Now they do, in CI.**
`tests/test_delivery.py` starts a real SMTP server (`tests/smtp_sink.py`) and an HTTP
sink, then asserts the whole `deliver()` path. Three bugs came straight out of it:
- `send_email` called `smtp.login()` unconditionally → `SMTPNotSupportedError` against
  anything without AUTH (MailHog/Mailpit/CI relays, IP-allowlisted SES). Now: STARTTLS
  and login are both opportunistic (`has_extn`), with a warning.
- the HTML email shipped `href="#"` "not interested" and "unsubscribe" links that go
  nowhere, plus an `open dashboard` link pointing at `""` when `notify.base_url` is
  unset. Dead controls that look live are worse than none: they are now rendered only
  with a real target, and opt-out tokens print as text instead of a fake button.
- `title=None`-style writes to the JSON-in-`Text` columns (`coverage`,
  `eligible_countries`, `gate_reasons`, …) died deep inside SQLite with
  `type 'list' is not supported` for any caller that was not the pipeline. A
  `@validates` on those columns now encodes list/dict/set/tuple on assignment.
Verified end-to-end: one SMTP transaction producing a parseable
`multipart/alternative` (plain + HTML, 78-char lines, no raw `<script>`, dashboard
link present), a Slack-shaped webhook body, a chunked Telegram `sendMessage`, and
four `notification_log` rows — plus `force=True` overriding both quiet hours and the
dedupe guard, and the console being exempt from quiet hours.

## H. One number per row: how the money engine actually decides (38–39)

**38. `amount_usd` is a gate, so every rule is written to under-state, never to
over-state.** The field decides whether a row passes `min_award_usd`, and the
digest prints it next to the source's own wording — which means a wrong 10× is
not a rounding error, it is a scholarship that looks fully funded when it is not.
The final contract in `parsers/fields.py`:

* The money regex matches **currency + number + a short tail**, and the tail
  refuses to cross into the *next* amount (`(?![^…]\s*(?:[$€£¥]|EUR|…)\s*\d)`
  on each character). That one guard is what stopped `US$50,000 plus a monthly
  allowance to cover living costs` from being read as `$500,000`.
* A unit counts only when it is **glued** to the number: the text between them
  must be a connector or the name of the payment, never an additive
  (`plus / and / or / with / ;`). So `€992 per month` and `$500 stipend per
  month` annualise; `a €7,000 grant; monthly costs extra` does not.
* A **range is priced as a pair**: `¥143,000–148,000 per month` writes its unit
  after the second bound, so the engine borrows it for the first and reports the
  **upper** bound (148,000 × JPY × 10 = US$9,768). Both bounds are never scaled,
  which would double-count the same offer.
* Monthly means **×10** (a term, not a calendar year) unless the page states a
  length — `for 24 months`, or `for two years`, which `_DURATION_RE` now reads
  as digits *or* words.
* `currency` is always an ISO code (`€`→EUR, `$`→USD, `HK$`→HKD) so the CSV and
  the gate key on one spelling; an unknown code keeps rate 1.0 rather than being
  guessed into a small currency and inflated.
* `amount_text` — what the digest actually shows — is the amount plus its unit,
  cut at the point the sentence moves to the next item, and with a word
  truncated by the tail cap dropped: `NZ$15,000 for undergraduate`, not
  `…undergraduate scholarsh`.

**39. Two "known limitations" that the suite used to *bless* are now fixed, and
one is still real.** `tests/test_money.py` had begun asserting the broken
behaviour (a range stored un-annualised, `¥143,000–148,000 per month` → 943.8)
with a docstring calling it deliberate. It was not: the cause was `\d[\d,.]{1,11}`
swallowing the range's second bound into the first number, which is a regex
grammar bug, not a design choice — fixed, and the test now asserts 9,768. Still
open, and asserted as such: `992 EUR, paid monthly` is not annualised (the words
are too far from the number to be trusted), and `€992/month (Master) or
€1,300/month (PhD)` reports the *upper* figure — a Master's applicant sees a
number worth about 20 % more than they would receive, which is why the digest
prints the source's own wording beside it.
