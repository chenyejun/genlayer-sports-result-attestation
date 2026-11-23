# SportsResultAttestation

A reusable on-chain registry of final sports scores: anyone requests
an attestation for a finished match (home team, away team, date)
together with two or three public result sources, validator consensus
fetches the score from those sources and agrees on it exactly, and
the contract stores the final score with its sources, so other
contracts and readers can use the attested result without re-running
the check.

## Why consensus is needed

A score reported by one party is a claim, not a fact. A single
submitter could report any score, and a single model call could
misread a page — or read a half-time score, or an aggregate across
two legs, and call it final. Here the score is produced by a leader
and re-produced independently by every validator, each fetching the
same two or three public sources, and it is only written to state
when both sides extract the same final score, goal for goal. The
winner is computed by deterministic code from the agreed goals, never
copied from model output, so the model's job is only to read numbers
off pages — it never decides the attested result.

## What it does

- `request_result` — request an attestation for a match (e.g.
  `Arsenal` vs `Chelsea` on `2026-11-22`) from 2–3 result source URLs
  (comma-separated). Returns the request id.
- `attest_result` — run validator consensus on a requested match and
  store the attestation: the final score (home and away goals as
  integers), the winner (`HOME` / `AWAY` / `DRAW`, derived from the
  goals), how many sources loaded, and the verification time.
- Views: `get_request`, `get_attestation`,
  `get_latest_attestation`, `list_requests`, `list_attestations`,
  `stats`.

A final score does not expire. Unlike a price, a finished match stays
finished, so attestations have no validity window: an old attestation
stays readable as history forever, and `get_latest_attestation`
always returns the newest record for the match. Re-requesting a match
is the refresh path — a later attestation just adds a newer record
for the same match.

## State design

| Field | Type | Purpose |
| --- | --- | --- |
| `requests` | `TreeMap[u256, ResultRequest]` | Requests by id |
| `attestations` | `TreeMap[u256, ResultAttestation]` | Attestations by id |
| `request_ids` / `attestation_ids` | `DynArray[u256]` | Insertion order for listing and for scanning back to the latest attestation of a match |
| `request_counter` / `attestation_counter` | `u256` | Monotonic ids |

`ResultRequest` stores the teams, the date, the match key (uppercased
`HOME vs AWAY` plus the date), the sources, the requester, the request
time, and the status (`REQUESTED` / `ATTESTED`). `ResultAttestation`
stores the request id, the teams and date, the agreed goals, the
derived winner, the sources actually checked, the verification time,
and the loaded/failed source counts.

Two rules shape the design. First, a request is attested once: after
`attest_result` the request is `ATTESTED`, and a fresh record needs a
new request — there is no path that overwrites an attestation, so
every stored score stays auditable. Second, the match index is a plain
scan backwards over `attestation_ids` rather than a `TreeMap[str,
DynArray[u256]]` keyed by match: the nested-collection form is an
unproven construct in this runner, and the scan is short in practice.

## How the equivalence check works

Attestation runs one consensus round over the same judge code
(`_judge`: fetch each source with `gl.nondet.web.get`, ask the model
for one score per source with `gl.nondet.exec_prompt` and
`response_format="json"`, validate the answer, require all loaded
sources to report the same score). The round is
`gl.vm.run_nondet_unsafe(leader_fn, validator_fn)`.

**The leader and the validator each do the whole job.** Both sides
fetch the sources, extract one score per source (home goals, away
goals, and whether the page shows the match as finished), and compare
in deterministic code. The stored score is the one both sides
agreed on.

**Agreement is exact, goal for goal.** The validator requires the
same status (`FINISHED`) and the same home and away goal counts on
both sides, compared as parsed integers with plain equality. There is
no tolerance band: a final score is exact by nature, and either both
sides read the same final score or nothing is stored. That is also why
the sources must agree with each other inside one side's extraction —
if two feeds report different final scores, the judge fails with
`[LLM_ERROR]` instead of picking one.

**A match that has not finished stores nothing.** If every loaded
source reports the match as scheduled, live, or postponed, the round
fails with `[EXPECTED]` ("match has not finished yet") — a
deterministic failure both sides agree on exactly, so the request
stays `REQUESTED` and can be attested later, after the match ends.
If no source reports the match at all, the round fails the same way
("sources do not report this match").

**Two of three sources is enough.** Each source is fetched
independently and a failure is skipped, not fatal: with two sources
loaded the score is attested from those two; with fewer than two the
round fails. The per-source failure class is recorded (`loaded` /
`failed` counts on the attestation), so a stored attestation says how
many sources backed it.

**Leader failures.** If the leader fails, the validator re-runs the
judge and compares failure classes: `[EXPECTED]` and `[EXTERNAL]` must
match exactly, `[TRANSIENT]` only has to be transient on both sides,
and `[LLM_ERROR]` never agrees, so a bad model answer rotates the
leader instead of locking bad state in.

Storage is never read inside a nondet block: `attest_result` copies
the request to memory with `gl.storage.copy_to_memory` first and hands
the consensus round plain in-memory values. The leader and validator
functions never reference `self`.

No `strict_eq` is used on any LLM output anywhere in the contract:
model-extracted goals are compared with plain integer equality, and
the match status is compared after normalizing the model's lowercase
answer to the internal constants.

## Failure paths

| Prefix | Meaning | Examples | Caller should |
| --- | --- | --- | --- |
| `[EXPECTED]` | Bad input or invalid state | Empty or over-long team name, malformed date, unknown request id, attesting an already-attested request, a match that has not finished yet, sources that do not report the match | Fix the input; for a match not finished yet, retry after it ends |
| `[EXTERNAL]` | The outside world is at fault, deterministically | A source returned 4xx (page gone, moved, or access denied); fewer than two sources loaded because of 4xx failures | Retry only if the source comes back; a dead source means the match cannot be attested from these endpoints |
| `[TRANSIENT]` | The outside world failed, possibly temporarily | A source returned 5xx or 429, or fewer than two sources loaded because of transient failures | Retry later; nothing was written |
| `[LLM_ERROR]` | The model answer was unusable | Non-JSON answer, a missing score for a loaded source, a score for a source that did not load, a negative or non-integer goal, a status outside `finished` / `not_finished` / `unknown`, sources disagreeing on the score | Retry; the leader rotates, so a persistent failure means the pages are the problem |

A failed `attest_result` writes nothing: the request stays
`REQUESTED` and the call can be retried.

## Methods

| Method | Access | Arguments | Returns |
| --- | --- | --- | --- |
| `request_result` | write | `home_team: str`, `away_team: str`, `match_date: str` (`YYYY-MM-DD`), `sources: str` (2–3 URLs, comma-separated) | `{request_id, status}` |
| `attest_result` | write | `request_id: u256` | `{attestation_id, request_id, home_team, away_team, match_date, home_goals, away_goals, winner, status}` |
| `get_request` | view | `request_id: u256` | Request dict |
| `get_attestation` | view | `attestation_id: u256` | Attestation dict |
| `get_latest_attestation` | view | `home_team: str`, `away_team: str`, `match_date: str` | Latest attestation for the match |
| `list_requests` | view | — | `{requests: [...]}` |
| `list_attestations` | view | — | `{attestations: [...]}` |
| `stats` | view | — | `{requests, attestations}` |

Notes on arguments: team names are stripped and internal whitespace
is collapsed (up to 60 characters); the match key is the uppercased
`HOME vs AWAY` plus the date, so the same match spelled two ways
still finds the same attestations. Dates must be `YYYY-MM-DD` with a
real month (01–12) and day (01–31). Goals are non-negative integers.
Sources are caller-chosen HTTPS URLs (2–3, distinct); the attestation
records exactly which sources the score came from, so a reader can
judge the sources for themselves.

## Deploying on StudioNet

Prerequisites: a GenLayer Studio account and the Studio app at
https://studio.genlayer.com/contracts.

1. Open Studio, switch to the StudioNet network, and create a new
   contract from `contracts/SportsResultAttestation.py` (paste the file
   or upload it).
2. Deploy with **no constructor arguments** — `__init__` takes none.
   The first line of the file pins the runner:
   `# { "Depends": "py-genlayer:1jb45aa8ynh2a9c9xn3b7qqh8sm5q93hwfp7jqmwsfhh8jpz09h6" }`.
   That is the hash the official project boilerplate pins
   (`genlayer-project-boilerplate`, `contracts/football_bets.py`), verified
   against the boilerplate on 2026-11-13. Keep the blank line after it:
   the runner parses the leading `#` lines as one JSON document, and a
   comment glued directly under the Depends line breaks deployment.
3. Note the deployed contract address. The full step-by-step checklist,
   including the exact calls to run after deploy and what each should
   return, is in `DEPLOY_NOTES.md`.

CLI alternative (same network, same file):

```bash
genlayer deploy --contract contracts/SportsResultAttestation.py
```

## Reading attestations from another contract

Use a typed interface for the methods you need. The interface is only
for type safety; at runtime it is the same as `gl.get_contract_at`.

```python
from genlayer import *


@gl.contract_interface
class SportsResultAttestationReader:
    @gl.public.view
    def get_latest_attestation(self, home_team: str, away_team: str, match_date: str) -> dict: ...


# ... inside a consumer contract, with the registry address in storage:
registry = gl.get_contract_at(SportsResultAttestationReader, self.registry)
latest = registry.get_latest_attestation("Arsenal", "Chelsea", "2026-11-22")
# latest["home_goals"] and latest["away_goals"] are the agreed final
# score; latest["winner"] is "HOME", "AWAY", or "DRAW".
```

Two things to keep in mind when composing. First, cross-contract calls
are asynchronous: the child call is created when the parent transaction
reaches `on='finalized'` by default, so do not write a consumer that
expects the score back in the same synchronous step — design around
the message flow (for example, read the attestation in a view first,
then act on it in a separate call). Second,
`get_latest_attestation` raises `[EXPECTED]` when the match has no
attestation yet; handle that in the consumer rather than assuming
every match is attested.

## Verifying after deploy

Run each write method once on Studio and check the result against
`DEPLOY_NOTES.md`. In short: request an attestation for a finished
match, read it back with `get_request`; attest it and confirm
`get_attestation` shows the final score from the result pages, the
derived winner, and `sources_loaded` of 2 or 3; request and attest a
second match and confirm `get_latest_attestation` returns the newer
attestation per match; confirm a live or upcoming match fails with
`[EXPECTED]` instead of storing a score.

## Tests

Direct tests (mocked web + LLM, no network) live in
`tests/direct/test_sports_result_attestation.py` and follow the official
`genlayer-test` pattern (`direct_vm` / `direct_deploy` fixtures). They
cover request validation and team/date normalization, a successful
attestation with the score and the derived winner (home win, draw, and
away win), validator agreement and disagreement (a different score
disagrees, a live match seen by the validator disagrees, via mock
swapping between leader and replay), two-of-three sources loading,
fewer than two loading (`[EXTERNAL]` for 4xx, `[TRANSIENT]` for 5xx),
a match not finished (`[EXPECTED]`), sources not reporting the match
(`[EXPECTED]`), sources disagreeing on the score (`[LLM_ERROR]`),
malformed model answers (`[LLM_ERROR]`: missing scores, a missing
entry for a loaded source, a negative goal, an invalid status), double
attestation and unknown ids, refresh creating a new attestation,
matches being independent of each other, and all views.

```bash
./.venv/bin/python -m pytest tests/direct -q
./.venv/bin/genvm-lint check contracts/SportsResultAttestation.py
```

## Limitations

- **Attestations are records of finished matches, not live scores.**
  A match still in progress cannot be attested — request it again
  after full time. The contract never refreshes itself.
- **Sources are chosen by the requester, not the contract.** The
  requester picks the 2–3 result pages; the attestation records
  exactly which ones, so a reader can judge them — but a requester
  who picks three pages that all repeat the same wrong score gets
  that score attested. Readers who care should check the sources on
  the attestation, not just the score.
- **Pages must carry the score early.** The judge reads at most the
  first 6000 characters of each page, so a result page that buries
  the final score deep in the markup (or behind scripts that never
  render server-side) cannot be attested. JSON result endpoints work
  best; heavy HTML pages may need a shorter URL form.
- **Exact agreement is strict by design.** Two sources that disagree
  on the score — one stale, one updated — fail the round instead of
  storing either number. That is the safe failure, but it means a
  match whose feeds update at different speeds may need a retry after
  the feeds settle.
- **Runner version.** The contract pins the boilerplate's runner hash
  (`1jb45aa8...`, rc7-era APIs such as `gl.vm.run_nondet_unsafe` and
  `gl.get_contract_at`). The SDK's current docs describe a newer runner
  with renamed APIs (`run_nondet`, `gl.contract.get_at`, and others);
  moving to it requires updating the consensus calls, not just the hash.
