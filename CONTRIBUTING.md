# Contributing

Thanks for taking a look. This is a small project, so the rules are short:
get it running, keep the tests green, and keep runs free unless a change
really needs an LLM call.

## Setup

```bash
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt -r requirements-dev.txt
cp .env.example .env   # optional - a run works with an empty .env
python cli.py status
```

Python 3.12 is what CI uses.

## Running the tests

```bash
python -m pytest tests -q
ruff check . --select E,F,B,C4,SIM,UP --ignore E501
```

The suite is offline by design: HTML fixtures under `tests/fixtures/` and a
local mock LLM server, so tests should never hit the network or cost money.
If you add a parser behaviour, add a fixture and a test next to it.

## Adding a source

Most sources need no Python. Add a block to `config.yaml` (see "Sources" in
the README for the five kinds), then debug it in isolation before a full run:

```bash
python cli.py test-source my_blog --limit 3 --show-text
python cli.py run --no-notify
```

Please don't enable a source by default until it has run cleanly twice -
a broken source is worse than a missing one, and the nightly job has no
human watching it.

## Pull requests

- One change per PR, with the test output in the description.
- If your change touches scoring or eligibility gates, say so in the title:
  those change what people get notified about.
- Don't commit `data/` artefacts (DB, cache, generated reports) unless the
  change is about the data itself.
