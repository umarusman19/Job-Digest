# Postmortem: a job watcher that cried wolf

This is the writeup of why the previous version of this system was replaced.
It is the reason the code in `src/` is shaped the way it is, and it is the part
of this repository I would want someone to read first.

## Summary

Between 6 September and 14 September 2026 I ran a job watcher built as a
scheduled LLM task: a natural-language prompt on a timer, told to read a set of
job boards and email me anything new. It failed three separate times, in three
different ways, and each failure was invisible until I went looking for it.

The rewrite in this repository is not a new feature. It is the same product
with the judgement moved out of a prompt and into code.

## Failure 1 — the silent scrape

The first version read boards through a third-party scraping actor. The actor
expected each target as an object pairing an ATS vendor with a company slug. I
passed bare company names.

The actor accepted the input, scraped nothing, and returned zero rows. Zero
rows is exactly what a legitimately quiet week looks like. Nine consecutive
runs reported no new jobs and every one of them was correct about the number
and wrong about the reason.

**What I got wrong.** I treated an empty result as evidence about the job
market rather than as an ambiguous signal that needed disambiguating.

**What the code does now.** A 404 is treated as a permanent error and reported
as *"board slug is probably wrong, which is NOT the same as 'no open roles'"*
(`sources.py::_get_json`). A run reports how many sources it actually read —
"9 of 12 sources read" — so zero jobs from twelve good sources reads
differently from zero jobs from zero good sources. There is a test for the
distinction.

## Failure 2 — the device that was asleep

The watcher was bound to my laptop. On 6 September it fired while the machine
was asleep, failed in eight seconds, and auto-suspended itself with reason
`device_absent`. It then never ran again. I found out two days later.

**What I got wrong.** I put a scheduled job on a device that is not always on,
and I had no signal for "this job has stopped existing". A monitoring system
that can silently stop is worse than no monitoring system, because you keep
believing you are covered.

**What the code does now.** It runs on GitHub Actions
(`.github/workflows/digest.yml`), which is always on, and the run history is
public. If it stops running, the gap is visible on the repository to me and to
anybody else. The workflow comment says this in more detail.

## Failure 3 — the one that actually mattered

This is the interesting one.

The replacement ran in the cloud and did deliver mail. Between 13 September
08:11 and 14 September 08:13 it sent at least six emails saying *boards
blocked / run failed / 0 boards read*. Each was followed roughly ten minutes
later by another email from the same task saying nothing had been wrong and the
boards had loaded fine. One of its own corrections described the mistake as
*"the same mistake this watcher has now made seven times."*

The mechanism: fetch refused, one retry, give up, **send a failure email**,
then retry properly, succeed, **send a correction email**.

**What I got wrong, and it is not the retry logic.** Six false alarms a day
taught me to ignore the digest. That is the only failure a notification system
cannot survive. A watcher that misses a job costs me one job. A watcher I stop
opening costs me all of them. I had built a thing whose failure mode was to
destroy its own credibility, and I had built it that way by giving it
permission to send more than one message per run.

The deeper cause is that "decide whether the fetch failed" was a sentence in a
prompt. An LLM asked to make that judgement will answer differently on
different runs against identical conditions. The non-determinism was the
feature I had accidentally specified.

**What the code does now.** Three changes, in order of importance:

1. **One run sends at most one email, and it always contains everything.**
   Source failures are a section inside the normal digest, never a message of
   their own. `render.py` says this at the top and
   `test_digest_never_splits_failures_into_a_separate_message` enforces it.
2. **Fetching cannot notify.** `fetch_board` returns a `SourceResult` with
   either postings or an error string. It never raises and never sends
   anything. Success and failure are both just data handed to the caller.
3. **Retries are explicit.** Three attempts, exponential backoff, and a
   documented distinction between transient (429, 5xx, timeout) and permanent
   (404) failures. No model is asked to form an opinion about any of it.

A partial read now exits 0 and still sends the digest. Only a run where
*every* source failed exits non-zero, because that is categorically different
— it usually means no network at all — and it is the one case worth turning the
Actions run red.

## The finding that came out of it

Instrumenting the mailbox (`inbox.py`) was meant to be a convenience. It
produced the most useful thing I learned in six weeks of applying.

Every rejection I received arrived inside two days of applying. Some inside
twelve hours. No human reads a portfolio, forms a view, and declines in twelve
hours. Those are automated screens, which means the thing rejecting me was
never looking at my work — it was looking at eligibility, location, and
keywords.

I had spent that period improving work samples. The measurement said the
bottleneck was upstream of anyone seeing them. That changed what I did next,
and I would not have known it without counting.

This is also why `inbox.py` reports its own uncertainty in the digest
(*"classification is phrase-matching and will misread unusual wording"*). A
number presented without its error bars is how the first version got me into
this.

## The pattern across all three

Each failure was a system that was confidently wrong in a way I could not see
from the outside:

- zero rows that looked like a quiet market
- a suspended job that looked like a quiet market
- a failure alert that looked like a real failure

The fix in every case was the same shape: make the system report what it
actually did, not what it concluded. "9 of 12 sources read, 47 postings, 46
filtered, 1 new" is checkable. "No new jobs" is not.

## What I would still change

- Classification is regex phrase-matching. It will misread unusual wording,
  and I would rather improve the corpus of phrases than reach for a model,
  because a model reintroduces the non-determinism this whole document is about.
- One row per company is wrong if you apply to two roles at the same company.
- There is no test against a real recorded mailbox, because I have not built a
  way to anonymise one that I trust.
