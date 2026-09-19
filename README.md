# Scholarship Tracker Agent

An autonomous agent that watches scholarship sources, turns messy pages into
clean structured rows, gates them against *your* eligibility, ranks them, and
notifies you only when something actually changed. Runs free on a GitHub
Actions schedule, keeps its memory in SQLite, and publishes a static dashboard.

```
sources ──▶ polite fetch + HTML cache ──▶ label/regex extraction (free)
                                                │
                                       triage: gaps only?
                                                │
                                     LLM (batched, cached, budgeted)
                                                │
                          validate (pydantic) ──▶ dedupe ──▶ SQLite
                                                                │
                                     hard eligibility gates ──▶ weighted score × confidence
                                                                │
                                        digest ──▶ console / email / Telegram / webhook
                                        report ──▶ data/out/{index.html,data.json,csv,md}
```

---

## 60-second start

```bash
cd scholarship-bot
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env            # optional — a run works with an empty .env

python cli.py status            # what would run, with what config
python cli.py test-source scholars4dev_masters --limit 3     # live, no writes
python cli.py run --no-notify   # scrape → store → score → report
python cli.py query --min-score 60 --explain
python cli.py dashboard         # http://localhost:8000
```

No API key? Nothing breaks. Labelled sites (Scholars4Dev and friends) are
parsed by the deterministic extractor; in a live run against
`scholars4dev_masters` + the curated funder file, **16/16 rows came out at 100%
confidence with 0 LLM calls and $0.000 spent.**

With a key in `.env` (`OPENAI_API_KEY=...`), the model is only called for rows
where ≥2 key fields are missing, and its answers are validated by the same
schema as everything else.

## What you configure

Everything lives in **`config.yaml`**; secrets live in **`.env`**.

| block | what it controls |
|---|---|
| `profile` | who you are: degree, GPA, IELTS, age, home country, fields, preferred countries, funding need |
| `scoring` | soft-score weights and tier cut-offs |
| `llm` | enabled/disabled, `fill_gaps_only`, model, base_url (any OpenAI-compatible endpoint), per-cycle `budget_usd` |
| `notify` | channels, score threshold, per-source caps, quiet hours, SMTP/Telegram/webhook |
| `runtime` | DB path, cache dir, politeness delays, `respect_robots`, stale-day sweep |
| `sources` | one entry per website, declaratively (see below) |

Env vars override YAML: `SCHOLARSHIP__profile__ielts=8.0`,
`SCHOLARSHIP__llm__enabled=false`, `SCHOLARSHIP__notify__dry_run=true`.

## Sources: five kinds, no Python needed to add one

| `kind` | use it when | example |
|---|---|---|
| `listing` | site publishes `.post` / card blocks with a link + title | Scholars4Dev |
| `table` | one row per scholarship with deadline/value/country columns | ScholarshipDB-style aggregators |
| `rss` | site offers a feed — **always prefer this**, it never breaks on redesign | any WordPress blog |
| `json_api` | an open-data endpoint exists | EACEA Erasmus+ projects API |
| `seed` | flagship programme that blocks datacenter IPs (DAAD, Chevening, Fulbright…) | `seeds/major_funders.json` |

Add a source by pasting a block into `config.yaml`:

```yaml
  - id: my_blog
    kind: listing
    enabled: true
    priority: 1
    url: https://example.com/scholarships/
    page_url: "{url}page/{page}/"      # pagination template
    pages: 2
    item_selector: "article"
    link_selector: "h2 a"
    title_selector: "h2"
    fetch_detail: true                  # only if these are missing…
    detail_when_missing: [funding_type, amount_usd, coverage]
    min_listing_score: 0
    max_detail_fetches: 40
```

Debug it in isolation, without touching the DB:

```bash
python cli.py test-source my_blog --limit 3 --show-text
```

### Sources currently enabled (7)

| id | what it covers | rows/night | notes |
|---|---|---|---|
| `scholars4dev_masters` | master's list, 2 pages | 20 | the highest-signal source: every item is one scholarship with a `Deadline:` line |
| `scholars4dev_phd` | PhD list | 10 | overlaps masters on cross-cutting awards — merged, not duplicated |
| `scholars4dev_pakistan` | Pakistan-focused search feed | 10 | WordPress search, re-ranked by our score, not their relevance |
| `scholars4dev_undergraduate` | bachelor's list | 10 | only useful if you also consider undergrad top-ups |
| `scholars4dev_engineering` | engineering/CS list | 10 | best field match for AI/ML applicants |
| `scholars4dev_germany` | Germany country page | 10 | Germany-scoped, catches DAAD items the level pages skip |
| `major_funders` | 17 curated flagship programmes | 17 | `seed` kind, with live `verify_url` re-reads |

Six live pages → **67 rows in 189 s on a cold DB**, then **21 s on a warm DB with
0 new rows**. Overlap is real and useful: 15 of those rows are credited to 2–4
sources at once (`seen_in_sources`), which is how you tell "listed everywhere"
from "one blog's copy-paste".

### Sites that are deliberately *not* enabled

Measured from this machine (worth re-checking from a home IP before you change them):

| site | result | decision |
|---|---|---|
| `daad.de` scholarships finder | HTTP 403 | `enabled: false`; DAAD EPOS lives in the seed file |
| `chevening.org` | 403 / timeout | seed file instead |
| `scholarshipdb.net` | TLS error | `table` scraper is written and fixture-tested; flip it on from home |
| `api.eacea.ec.europa.eu` | does not resolve | `json_api` scraper written, `enabled: false` |
| `scholarshipportal.com` | HTTP 403 (Cloudflare) | not enabled |
| `euraxess.ec.europa.eu/jobs` | 200, server-rendered, 10 items with `Deadline: 19 Oct 2026 - 12:59` | **not** enabled: those are individual vacancies (assistant posts), not scholarship programmes — high noise for your profile |
| `studying-in-germany.org/scholarships/` | 200, 290 KB | one long prose article, no per-item markup → nothing to list |
| `brightscholar.com`, `edufind`, `istudies`, `findscholarships`, `applykwik`, `hec.gov.pk`, `studyinkorea`, `campuschina`, `stipendiumhungaricum` | 403 / DNS fail / JS shell / timeout | not enabled |

The `rss` scraper exists, but Scholars4Dev's own `/feed/` returns an empty
channel, so there is nothing to prefer there.

### The seed file is deliberate, not lazy

During the build, `daad.de`, `chevening.org`, `scholarshipdb.net`,
`campuschina.org` and `eacea` were all **403 / TLS-blocked from a datacenter
IP**. Pretending to scrape them yields "0 results" every night and you never
learn why. So `seeds/major_funders.json` holds 17 curated flagship programmes
(Chevening, Fulbright Pakistan, DAAD EPOS, EMJM, CSC, Türkiye Bursları, MEXT,
GKS, Stipendium Hungaricum, Commonwealth Shared, SIIS, AKF, Australia Awards…)
with:

* `verify_url` — **the bot fetches it every run and overrides deadline / amount
  / IELTS from the live page** when it can read them;
* `review_before` — once that date passes the row is flagged `stale seed` in the
  report, so you know the static part needs a manual refresh.

## Why the scoring is two-stage

`filters/matcher.py` in the original blueprint only *added points*. That emails
you awards you cannot get. Here:

1. **Hard gates** (disqualify, with a printed reason): citizenship not on the
   eligible list or on the excluded list · degree level mismatch · GPA below the
   stated minimum (scale-aware) · IELTS/TOEFL below the stated minimum · age
   limit · work-experience minimum · years-since-degree cap · deadline already
   passed · country you blocked.
2. **Weighted soft score** (0–100): country 22 · funding 18 · field 16 ·
   amount 12 · degree 10 · competition 8 · research 8 · english-readiness 6.
3. **Confidence factor** — `score × (0.7 + 0.3·confidence)`, so a half-parsed
   row can never outrank a clean one.
4. **Urgency nudge** — ×1.12 inside 14 days, ×1.05 inside 45, ×0.97 beyond
   ~8 months, and rows past deadline are pinned below the notification floor.

Every row keeps `score_breakdown` (JSON) and `gate_reasons`, so `--explain` and
the dashboard show *why*: `country 22, funding 18, field 14 …` / `⚠ partial
funding — check it covers your costs`.

## Cost control (the part that actually saves money)

| mechanism | effect |
|---|---|
| label/regex extraction first | ~90–100% of fields for $0 on labelled sites |
| `detail_when_missing` | detail page fetched only when funding/amount/coverage are absent |
| conditional GET + HTML cache | unchanged pages cost ~0 bytes and no parsing |
| `record_hash` skip | cosmetic site edits don't re-store or re-notify |
| triage (`fill_gaps_only`) | model called only when ≥2 key fields are missing |
| batching (`batch_size: 5`) | 5 items per request |
| `parse_cache.sqlite3` | identical content hash → instant, free |
| `budget_usd` per cycle | hard stop, then regex-only for the rest of the run |

Measured in this build (7 sources, ~100 listing candidates): first cold run
fetches ~150 pages, 0 LLM calls, $0.000; every later run fetches 6 listing
pages, **0 detail pages**, 0 LLM calls, $0.000, ~21 s total.

The second-level cheapness comes from `skip_known`: a listing row whose stored
page hash still matches never gets its detail page fetched at all. Disabling it
(`skip_known: false`) is the way to force a full re-read after you change the
parser and want to re-derive everything.

## CLI

```
status                       resolved config + which sources are enabled
test-source <id>             one source, live, shows fields found / score / gate reasons
run                          full cycle (--source, --limit, --offline, --force, --no-notify)
query                        -q, --tier, --country, --degree, --funding, --min-score, --soon, --explain
report                       rewrite data/out/{index.html,data.json,scholarships.csv,summary.md}
dashboard                    serve that folder locally (--rebuild)
health                       per-source reliability from the last run
notify-preview               render the digest that would be sent (--html, --mark-sent)
```

## Deploying on GitHub Actions (free)

1. Push this folder to a repo. Under **Settings → Pages**, choose *Deploy from a
   branch → `main` → `/docs`*. The nightly workflow writes `docs/index.html`, so
   your dashboard updates itself with no server.
2. Add secrets: `OPENAI_API_KEY` (or `LLM_BASE_URL`+`LLM_API_KEY`+`LLM_MODEL`),
   `SMTP_*`, `TELEGRAM_BOT_TOKEN`, `TELEGRAM_CHAT_ID`, `DASHBOARD_URL`,
   `DIGEST_SECRET`.
3. Actions → *nightly-scrape* → Run workflow to test. The step summary shows a
   per-source table; if a source breaks the workflow opens (and comments on) a
   `scraper-health` issue.
4. The DB and caches persist via `actions/cache` **and** are committed to `docs/`'s
   sibling `data/` path each night, so an expired Actions cache doesn't reset
   your dedupe memory.
5. Playwright is installed **only** when a source has `render: true` — saves
   ~2 min and 400 MB of every run.

Local scheduling instead: `python cli.py run` from cron/systemd, or keep the
`schedule` library loop (`while True: schedule.run_pending()`).

## Tests

```bash
pip install -r requirements-dev.txt
python -m pytest tests -q          # 129 tests, fully offline
```

Coverage: date/money/IELTS/GPA/label parsing, HTML cleaning, schema rejection of
hallucinated dates, every hard gate, score ordering, URL/title normalisation,
cross-source dedupe, idempotent re-runs, circuit breaker, cache, throttle, RSS /
table / listing / JSON-API scrapers against fixture HTML, source-config sanity
(every enabled source's kind/selectors/limits, bounded detail fetches), the
delivery path against a **real SMTP server and HTTP sinks** (multipart email,
escaping, quiet hours, caps, dedupe, Telegram chunking), digest rendering for
all channels, the LLM client against a **local mock OpenAI server** (batch,
cache, budget, malformed-output recovery, no-overwrite rule), and config loading
(every `profile:` key in config.yaml must map to a real field — that is how
`work_experience_years` being silently ignored got caught).

Lint: `python -m ruff check .` — clean apart from deliberate `BLE001`s (a scraper
that raises kills the whole run, so broad catches are the point).

## CI plumbing

`ci/wrap_run.py` + `ci/summarise.py` are the two scripts the nightly workflow calls after
the run. They exist so a broken scrape cannot look green:

| what happened | summary | exit code |
|---|---|---|
| all sources ok | per-source table | 0 |
| one 403 | table row shows the error + `failed_sources` | 0 (issue opened instead) |
| **every** source failed | table + `all_failed=true` | 1 |
| CLI crashed mid-run | ❌ crash card with the stderr tail | 1 |

`run --json` output is what they read, so you can test them locally:
`python cli.py run --json > run.json && python ci/summarise.py run.json`.

## Optional: an agent you can *ask* questions

`agent.py` shows a LangChain-free way to do the same thing with any chat model:
the model calls three functions (`search`, `deadlines_soon`, `why`) against the
SQLite DB and answers from real rows. Kept out of the nightly path on purpose —
free, deterministic, and no extra tokens in the loop. Without an API key it prints
the raw tool results instead, so you can use it as a CLI search anyway.

## Limits you should know

* Amounts use **static FX rates** (`parsers/fields.FX_TO_USD`) and a monthly
  stipend is annualised ×10 (a term, not a calendar year) — good for ranking,
  not for budgeting. A unit only counts when it is glued to its number, ranges
  are priced by the unit after their second bound, and anything ambiguous is
  deliberately left un-annualised: `amount_usd` gates `min_award_usd`, so it may
  understate but must not overstate. See `DEBRIEF.md` §H for the full contract.
* Aggregator deadline text like `14 Oct/8 Dec 2026` is read as one date; verify
  on the official page before relying on it. `deadline_estimated` and
  `quality_flags` tell you which rows are soft.
* `excluded_countries` / `eligible_countries` are only trusted when every entry
  resolves to a real country or known group — otherwise the gate is skipped and
  you get a warning instead of a wrong "ineligible".
* `respect_robots` defaults to `false` so you can run it at home; turn it on if
  you ever mirror this to a public domain.
