# Deploy notes — SportsResultAttestation

## Contract

- File: `contracts/SportsResultAttestation.py`
- Class: `SportsResultAttestation`
- Runner (line 1, pinned): `py-genlayer:1jb45aa8ynh2a9c9xn3b7qqh8sm5q93hwfp7jqmwsfhh8jpz09h6`
  — the hash the official project boilerplate pins
  (`genlayer-project-boilerplate`, `contracts/football_bets.py`), verified
  against the boilerplate on 2026-11-13. Keep the blank line after it.
- `genvm-lint check` passes (3 checks) and validation passes: 8 methods
  (6 view, 2 write). Note: the linter reports a newer runner is available
  (`1zr6nqk...`); the contract targets the boilerplate's runner, and
  moving to a newer runner requires the renamed APIs, not just the hash —
  do not change the hash alone.
- Direct tests: 22 passed
  (`./.venv/bin/python -m pytest tests/direct -q`).

## Constructor

**No constructor arguments.** `__init__` takes none and only zeroes the
two counters. Deploy from Studio (https://studio.genlayer.com/contracts,
StudioNet network) or the CLI:

```bash
genlayer deploy --contract contracts/SportsResultAttestation.py
```

No value needs to be sent with any call. Consensus rounds (fetching
pages + model calls) run on the leader and validators; Studio shows the
fee estimate before you confirm — if an `attest_result` fee looks wrong,
do not confirm; re-check the match first.

## Before you start: pick the match and the sources

Scores change from match to match, so the expected values below are
shapes, not fixed numbers: **before each `attest_result`, check the
final score of the match on both sources and confirm the attested
score matches what the sources agree on.**

- Pick a match that finished in the last few days (a live or upcoming
  match fails with `[EXPECTED]` by design — that is step 5, not a
  failure of the contract).
- Use 2–3 result sources that show the final score near the top of the
  page. JSON result endpoints work best; the judge reads at most the
  first 6000 characters of each page. Known-good patterns:
  - ESPN scoreboard API:
    `https://site.api.espn.com/apis/site/v2/sports/soccer/eng.1/scoreboard?dates=YYYYMMDD`
    (replace `eng.1` with the league and `YYYYMMDD` with the match
    date; the JSON carries the final score per event).
  - BBC Sport results page for the date, e.g.
    `https://www.bbc.com/sport/football/scores/YYYY-MM-DD`.
  - A Flashscore match page, e.g.
    `https://www.flashscore.com/match/<match-slug>/`.
- Example from the weekend of the batch (verify first — check the live
  pages before attesting, do not trust this note): Arsenal vs Chelsea,
  Premier League, 2026-11-22, reported 2–1 to Arsenal. If the live
  pages show a different score, use what they show.

## Post-deploy checklist

### 1. `request_result` — happy path

Args:

- `home_team`: `Arsenal`
- `away_team`: `Chelsea`
- `match_date`: `2026-11-22`
- `sources`: the 2–3 URLs from the section above, comma-separated

Expected: returns `{request_id: 1, status: "REQUESTED"}`. Then call
`get_request(1)` and confirm `home_team: "Arsenal"`, `away_team:
"Chelsea"`, `match_date: "2026-11-22"`, `match_key: "ARSENAL vs
CHELSEA 2026-11-22"`, `sources` lists the URLs given, and `requester`
is your address.

### 2. `request_result` — team normalization

Args: `home_team`: `  arsenal  `, `away_team`: `Chelsea`,
`match_date`: `2026-11-22`, same sources.

Expected: returns a new request id (2) with `home_team: "arsenal"`
(whitespace collapsed) and the same `match_key` as request 1 — the
key is case-insensitive. A second request for the same match is
allowed; each request is attested independently.

### 3. `request_result` — bad input rejected

- `match_date`: `2026-13-01`. Expected: reverts with `[EXPECTED]`
  (month must be 01–12).
- `match_date`: `22/11/2026`. Expected: reverts with `[EXPECTED]`
  (must look like YYYY-MM-DD).
- One source only. Expected: reverts with `[EXPECTED]` (at least two
  source URLs are required).

### 4. `attest_result` — happy path

Args: `request_id: 1`.

Expected: returns `{attestation_id: 1, request_id: 1, home_team:
"Arsenal", away_team: "Chelsea", match_date: "2026-11-22",
home_goals: <final home goals>, away_goals: <final away goals>,
winner: "HOME" | "AWAY" | "DRAW", status: "ATTESTED"}`. The goals must
match the final score both sources agree on — check the live pages
first and accept only that score. Then call `get_attestation(1)` and
confirm: `home_goals` / `away_goals` match the returned score,
`winner` is derived from them (more home goals → `HOME`, more away
goals → `AWAY`, equal → `DRAW`), `sources` lists the URLs checked,
`sources_loaded` is 2 or 3. Call `get_request(1)` and confirm `status:
"ATTESTED"`.

If it reverts with `[TRANSIENT]`, a source was briefly unreachable —
wait and retry the same call. If it reverts with `[EXTERNAL]`, a source
endpoint is down or returns 4xx — check the URLs in a browser and
retry when they load. If it reverts with `[LLM_ERROR]`, the model
returned a malformed answer or the sources disagreed on the score —
retry once; if it persists, the feeds may still be settling (one
updated, one stale) — wait and retry later.

### 5. `attest_result` — match not finished

1. `request_result` for a match that has not finished yet (a fixture
   later this week, same 2–3 sources) → `{request_id: 3, ...}`.
2. `attest_result(3)`.

Expected: reverts with `[EXPECTED]` (`match has not finished yet`).
The request stays `REQUESTED` — nothing was written, and the same
request can be attested after the match ends.

### 6. `attest_result` — already attested

Args: `request_id: 1`.

Expected: reverts with `[EXPECTED]` (`request 1 is already ATTESTED`).

### 7. `get_latest_attestation` and views — happy path

- `get_latest_attestation("Arsenal", "Chelsea", "2026-11-22")` →
  attestation 1, with the agreed score and winner.
- `get_latest_attestation("Real Madrid", "Barcelona", "2026-11-22")`
  → reverts with `[EXPECTED]` (no attestation for that match).
- `get_request(999)` / `get_attestation(999)` → revert with
  `[EXPECTED]` (unknown id).

### 8. Second match — independence

1. `request_result("Liverpool", "Manchester City", "2026-11-21",
   <sources>)` → `{request_id: 4, ...}`.
2. `attest_result(4)` → expect `{attestation_id: 2, home_goals: <as
   the sources report>, away_goals: <as the sources report>}`.
3. `get_latest_attestation("Liverpool", "Manchester City",
   "2026-11-21")` → attestation 2.
4. `get_latest_attestation("Arsenal", "Chelsea", "2026-11-22")` →
   still attestation 1 — the two matches are independent.

### 9. Views (read-only, no fee)

- `list_requests()` → all requests (4 after step 8).
- `list_attestations()` → all attestations (2 after step 8).
- `stats()` → `{requests: 4, attestations: 2}` (numbers grow if you
  ran extra requests).

## If something looks wrong

- Deploy rejected with `invalid_contract`: the Depends line is the
  usual suspect — confirm line 1 is exactly the pinned hash and line 2
  is blank.
- `attest_result` reverts `[LLM_ERROR]`: the model returned something
  outside the expected shape, or the sources disagreed on the score.
  Retry once; if it persists, open the source URLs and check whether
  they actually agree on a final score for this match — a page that
  shows a half-time score, an aggregate across two legs, or a
  different match with similar team names will not attest.
- `attest_result` reverts `[EXPECTED]` with "match has not finished
  yet": the match is still live or scheduled. That is the contract
  working as designed — wait for full time and retry the same request.
- `attest_result` reverts `[TRANSIENT]` repeatedly: one of the public
  endpoints is having a bad day. The round needs at least two sources;
  check the endpoints in a browser and retry when at least two are up.
- `attest_result` hangs or times out on Studio: the consensus round is
  waiting on validators. Wait for the Studio timeout message; nothing
  was written. Retry.
- The attested score is not the final score: do not trust it — check
  `get_attestation` to see which sources were checked. A score from
  only two sources during a live update can lag; request a fresh
  attestation once the feeds have settled.
