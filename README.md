# job-digest

A job watcher that reads ATS boards directly, emails me only genuinely new
roles, and measures what happens after I apply.

It replaces a version of itself that failed three times in nine days. The
interesting part of this repository is not the scraper — it is
**[docs/postmortem.md](docs/postmortem.md)**, which is the writeup of those
failures and the reason every design decision here is what it is.

```
                      ┌──────────────┐
  Greenhouse ───┐     │              │
  Lever      ───┼───▶ │ fetch_all()  │──▶ postings + per-source errors
  Ashby      ───┘     │ never raises │
                      └──────────────┘
                              │
                      ┌───────▼───────┐
                      │ filters       │  pure functions, word-boundary
                      │ + drop reasons│  matching, exclusions first
                      └───────┬───────┘
                              │
                      ┌───────▼───────┐
                      │ store         │  set difference against a JSON file,
                      │ "what's new"  │  not a model's opinion
                      └───────┬───────┘
                              │
  your mailbox ──▶ inbox ─────┤          aggregate stats only, names never
   (IMAP, read-only)          │          leave the process
                              │
                      ┌───────▼───────┐
                      │ render        │  ONE email per run. Always.
                      └───────────────┘
```

## Why it exists

I was laid off in September 2026 and started applying in volume. Two problems
showed up immediately. I was seeing the same roles repeatedly and missing new
ones, and I had no idea what was happening to the applications I sent.

The first version of this was a scheduled LLM task — a prompt on a timer told
to read the boards and email me. It broke in three ways: it scraped nothing for
nine runs while reporting "no new jobs"; it auto-suspended itself when my
laptop slept and never ran again; and then it sent six false "run failed"
alerts in two days, each followed by its own correction ten minutes later.

That third one is the one that mattered. Six false alarms a day taught me to
stop opening the digest, and a notification system I don't open is worth less
than nothing. The root cause was that "decide whether the fetch failed" was a
sentence in a prompt, and an LLM asked to make that judgement answers
differently on identical inputs.

So this version moves every judgement into code. Same product, deterministic.

## What it does

**Reads boards from public JSON endpoints.** Greenhouse, Lever and Ashby all
expose their job boards as unauthenticated JSON. No API keys, no scraping, no
headless browser, nothing that can be rate-limited into looking like a bug.

**Reports what it did, not what it concluded.** Every digest opens with
`9 of 12 sources read. 3 new roles after filtering.` and ends with a histogram
of why things were dropped. Zero results from twelve working sources reads
differently from zero results from zero working sources — the previous version
could not tell those apart, and neither could I.

**Sends exactly one email per run.** Source failures appear as a section
inside the normal digest. There is no alert path. This is enforced by
`test_digest_never_splits_failures_into_a_separate_message`.

**Tracks applications from my own mailbox.** Over IMAP, read-only, classifying
threads into awaiting / rejected / advanced / ghosted and reporting time-to-
response.

That last one produced the finding that changed how I apply: **every rejection
arrived within two days, some within twelve hours.** Nobody reads a portfolio,
forms a view, and declines in twelve hours. Those are automated screens, which
means the thing rejecting me was never looking at my work — it was looking at
eligibility, location and keywords. I had been spending my effort on work
samples. The measurement said the bottleneck was upstream of anyone seeing them.

## Privacy, because this repo is public and my mailbox is not

The boundary is enforced in code rather than by remembering:

| Object | Holds | Where it can go |
|---|---|---|
| `Application` | company, subject line | memory only, long enough to render one email |
| `Stats` | counts and durations | disk, commits, this README |

`Stats` has no string fields at all, and `to_public_dict()` uses an explicit
key whitelist rather than `dataclasses.asdict()`, so a future edit that adds a
field does not silently start exporting it. Three tests hold that line:

- `test_public_stats_carry_no_identifiers` — sentinel company names must not
  appear in the serialised payload
- `test_stats_has_no_string_fields_at_all` — structural, so a leak is
  impossible rather than merely absent
- `test_state_file_holds_no_titles_or_companies` — the committed state file
  stores opaque ids and dates only

`config.yaml` is gitignored. A public target list says more about a private job
search than I want to publish, so the real one lives in a repository secret and
`config.example.yaml` ships with placeholder companies.

Credentials are never in this repository and never in config. The workflow
reads `GMAIL_USER` and `GMAIL_APP_PASSWORD` from Actions secrets, set by hand.
Use a Google App Password.

## Running it

```bash
git clone https://github.com/umarusman19/Job-Digest && cd Job-Digest
pip install -r requirements.txt
cp config.example.yaml config.yaml    # then edit it

# print the digest, send nothing, touch no state
python -m src.run --config config.yaml --dry-run --skip-inbox

# for real
export GMAIL_USER=you@gmail.com
export GMAIL_APP_PASSWORD=...        # Google App Password, not your password
python -m src.run --config config.yaml
```

Exit codes: `0` ran and sent, `1` config or credential problem, `2` every
single source failed. A run where *some* sources failed still exits 0 and still
sends — that choice is the whole point of the rewrite.

### On a schedule

`.github/workflows/digest.yml` runs it twice daily on GitHub Actions. Set three
repository secrets: `CONFIG_YAML` (the whole file), `GMAIL_USER`,
`GMAIL_APP_PASSWORD`.

It runs on Actions rather than on my machine because the first version died
`device_absent` when my laptop slept and nobody noticed for two days. Actions
is always on and its run history is public, which means "this works" is
checkable by anyone reading the repo instead of something I assert here.

## Tests

```bash
python -m pytest -q     # 54 tests, no network
```

The adapters are pure functions over JSON, so they are tested against
fixtures. Live endpoints are checked by a separate `smoke` job in the workflow
which is allowed to fail: a third party changing their API should be a warning
on the run, not a red mark on my commit.

Tests worth reading, if you only read a few:

- `test_digest_never_splits_failures_into_a_separate_message` — the regression
  test for the bug this rewrite exists to fix
- `test_public_stats_carry_no_identifiers` — the redaction boundary
- `test_word_boundaries_ui_does_not_match_building` — `"ui"` in the exclusion
  list was matching `building` and `guide`, which is how a filter quietly
  stops filtering
- `test_rejection_beats_advance_when_both_present` — rejection emails
  routinely say "next steps" in the sign-off; if the positive classifier won,
  every such rejection would count as progress

## Known limitations

I would rather state these than have someone find them.

- **The endpoint shapes are unverified from where I built this.** The sandbox I
  wrote it in blocks outbound requests to all three ATS hosts by policy, so the
  adapters are correct against recorded fixtures and the first Actions run is
  the first live test. The `smoke` job exists for exactly this reason.
- **Classification is regex phrase-matching**, not a classifier. It will
  misread unusual wording, and the digest says so in its own output. I am not
  reaching for a model here, because a model reintroduces the non-determinism
  this entire repository is a reaction to.
- **One row per company**, so applying to two roles at the same company
  collapses into one.
- **Three ATS vendors.** Workday and SmartRecruiters are the obvious gaps and
  both are materially harder.
- **No test against a real recorded mailbox**, because I have not built an
  anonymisation step I trust.

## Licence

MIT. See [LICENSE](LICENSE).
