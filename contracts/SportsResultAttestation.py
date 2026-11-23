# { "Depends": "py-genlayer:1jb45aa8ynh2a9c9xn3b7qqh8sm5q93hwfp7jqmwsfhh8jpz09h6" }

"""SportsResultAttestation — a final-score attestation registry.

Anyone requests an attestation for a finished match (home team, away
team, date) together with two or three public result sources.
Validator consensus fetches those sources, extracts the final score
from each, and agrees on it. The agreed score is stored with its
sources, so other contracts and readers can check what a match ended
without trusting a single feed or a single caller.

Why consensus, and why exact agreement: a final score is two small
integers, not a judgement, so agreement is exact. The leader and each
validator independently extract one score per source and the contract
compares the parsed integers in deterministic code — the model never
outputs the agreed score itself, only the per-source facts, and no
strict_eq is ever used on model output. A round where the sources
disagree with each other, or where the match has not finished yet,
stores nothing: a score that is still moving, or that two feeds report
differently, is not a final score.

The winner is derived by deterministic code from the agreed goals,
never copied from model output: more home goals is a home win, more
away goals is an away win, equal goals is a draw.

A final score does not expire. Unlike a price, a finished match stays
finished, so attestations have no validity window: an old attestation
stays readable as history forever, and get_latest_attestation always
returns the newest one for the match. Re-requesting a match is the
refresh path, not an error — a later attestation supersedes nothing,
it just adds a newer record for the same match.
"""

import json
from dataclasses import dataclass
from datetime import datetime, timezone

from genlayer import *


# Error prefixes. Failures are raised as gl.vm.UserError with one of
# these so the leader and the validators can tell deterministic failures
# apart from transient ones: [EXPECTED] and [EXTERNAL] must match
# exactly, [TRANSIENT] only has to be transient on both sides, and
# [LLM_ERROR] never agrees, so a bad model answer rotates the leader
# instead of locking bad state in.
ERR_EXPECTED = "[EXPECTED]"
ERR_EXTERNAL = "[EXTERNAL]"
ERR_TRANSIENT = "[TRANSIENT]"
ERR_LLM = "[LLM_ERROR]"


# Bounds. A contract that reads the open web needs limits that do not
# depend on the pages behaving: every transaction has a compute budget,
# and the leader's return value is written to the chain.
MAX_TEAM_CHARS = 60
MAX_URL_CHARS = 500
MIN_SOURCES = 2
MAX_SOURCES = 3
MAX_PAGE_CHARS = 6000

STATUS_REQUESTED = "REQUESTED"
STATUS_ATTESTED = "ATTESTED"

MATCH_FINISHED = "FINISHED"
MATCH_NOT_FINISHED = "NOT_FINISHED"
MATCH_UNKNOWN = "UNKNOWN"

WINNER_HOME = "HOME"
WINNER_AWAY = "AWAY"
WINNER_DRAW = "DRAW"


@allow_storage
@dataclass
class ResultRequest:
    request_id: u256
    home_team: str
    away_team: str
    match_date: str
    # Lookup key: uppercased "HOME vs AWAY" plus the date, so the same
    # match spelled two ways does not create two registries.
    match_key: str
    # Source URLs, joined with "\n". URLs are validated to contain no
    # whitespace, so the join is unambiguous and reversible.
    sources: str
    requester: Address
    requested_at: u256
    status: str


@allow_storage
@dataclass
class ResultAttestation:
    attestation_id: u256
    request_id: u256
    home_team: str
    away_team: str
    match_date: str
    match_key: str
    # Agreed final score. Goals are small integers, stored as u256.
    home_goals: u256
    away_goals: u256
    # Derived by deterministic code from the goals, never from the
    # model: HOME / AWAY / DRAW.
    winner: str
    # Sources actually checked, joined with "\n".
    sources: str
    verified_by: Address
    verified_at: u256
    sources_loaded: u256
    sources_failed: u256


def _fail(prefix: str, detail: str) -> None:
    raise gl.vm.UserError(prefix + " " + detail)


def _now_seconds() -> u256:
    """Current transaction time as unix seconds.

    The GenVM wires the standard library clock to the transaction
    datetime, so every validator observes the same value.
    """
    return u256(int(datetime.now(timezone.utc).timestamp()))


def _as_u256(value, label: str) -> u256:
    """Coerce one numeric argument to u256.

    Studio's write form serializes numbers as JSON strings, and the
    pinned py-genlayer's u256() is a pass-through (typing.NewType), so
    it does not convert them. int() normalizes both forms (a no-op on
    values that are already ints). A string that is not a whole number
    raises a clean [EXPECTED] revert here instead of letting a bare
    ValueError escape the contract.
    """
    try:
        return u256(int(value))
    except (TypeError, ValueError):
        raise gl.vm.UserError("[EXPECTED] " + label + " must be a whole number")


def _clean_team(team: str, label: str) -> str:
    """Validate and normalize a team name: stripped, internal
    whitespace collapsed, 1-60 characters."""
    text = " ".join(str(team).split())
    if len(text) == 0:
        _fail(ERR_EXPECTED, label + " must not be empty")
    if len(text) > MAX_TEAM_CHARS:
        _fail(ERR_EXPECTED, label + " must be " + str(MAX_TEAM_CHARS) + " characters or fewer")
    return text


def _clean_date(match_date: str) -> str:
    """Validate a match date: YYYY-MM-DD with a real month and day.

    The date is part of the match key, so a malformed date must never
    create a request. The check is deliberately shallow — a calendar
    check, not a judgement about whether the match exists — because
    existence is what consensus verifies.
    """
    text = str(match_date).strip()
    if len(text) != 10 or text[4] != "-" or text[7] != "-":
        _fail(ERR_EXPECTED, "match date must look like YYYY-MM-DD, for example 2026-11-22")
    year = text[0:4]
    month = text[5:7]
    day = text[8:10]
    for part in (year, month, day):
        for ch in part:
            if not ("0" <= ch <= "9"):
                _fail(ERR_EXPECTED, "match date must look like YYYY-MM-DD, for example 2026-11-22")
    month_num = int(month)
    day_num = int(day)
    if month_num < 1 or month_num > 12:
        _fail(ERR_EXPECTED, "match date month must be 01-12")
    if day_num < 1 or day_num > 31:
        _fail(ERR_EXPECTED, "match date day must be 01-31")
    return text


def _match_key(home_team: str, away_team: str, match_date: str) -> str:
    return home_team.upper() + " vs " + away_team.upper() + " " + match_date


def _clean_url(url: str) -> str:
    """Validate one result source URL. HTTPS only, no credentials, no
    explicit port, no whitespace, and a dotted DNS hostname (never an
    IP literal). URL syntax alone never makes a source trustworthy —
    that is the requester's choice, and the attestation records exactly
    which sources the score came from."""
    text = str(url).strip()
    if len(text) == 0:
        _fail(ERR_EXPECTED, "source URL must not be empty")
    if len(text) > MAX_URL_CHARS:
        _fail(ERR_EXPECTED, "source URL must be " + str(MAX_URL_CHARS) + " characters or fewer")
    if not text.startswith("https://"):
        _fail(ERR_EXPECTED, "source URL must start with https://: " + text)
    if text != "".join(text.split()):
        _fail(ERR_EXPECTED, "source URL must not contain whitespace: " + text)
    rest = text[len("https://"):]
    authority_end = len(rest)
    for separator in ("/", "?", "#"):
        found = rest.find(separator)
        if found >= 0 and found < authority_end:
            authority_end = found
    authority = rest[:authority_end]
    if len(authority) == 0:
        _fail(ERR_EXPECTED, "source URL has no host: " + text)
    if "@" in authority:
        _fail(ERR_EXPECTED, "source URL must not contain credentials: " + text)
    if ":" in authority:
        _fail(ERR_EXPECTED, "source URL must not contain a port: " + text)
    host = authority.lower()
    if "." not in host:
        _fail(ERR_EXPECTED, "source URL host must be a dotted domain: " + text)
    return text


def _parse_sources(sources: str) -> list:
    """Split the comma-separated sources argument. The CLI passes
    strings, numbers and booleans only — a list has to go through a
    deploy script — so the public shape is one string and the splitting
    happens here."""
    parsed = [part.strip() for part in str(sources).split(",")]
    parsed = [part for part in parsed if part != ""]
    if len(parsed) < MIN_SOURCES:
        _fail(ERR_EXPECTED, "at least " + str(MIN_SOURCES) + " source URLs are required")
    if len(parsed) > MAX_SOURCES:
        _fail(ERR_EXPECTED, "at most " + str(MAX_SOURCES) + " source URLs, got " + str(len(parsed)))
    cleaned = [_clean_url(url) for url in parsed]
    if len(set(cleaned)) != len(cleaned):
        _fail(ERR_EXPECTED, "source URLs must be distinct")
    return cleaned


def _derive_winner(home_goals: u256, away_goals: u256) -> str:
    """Winner from the agreed goals, in deterministic code."""
    if home_goals > away_goals:
        return WINNER_HOME
    if away_goals > home_goals:
        return WINNER_AWAY
    return WINNER_DRAW


def _error_text(exc) -> str:
    """Read the message out of a UserError / Result, across the runner
    versions that name the payload `.data` or `.message`."""
    for attr in ("data", "message"):
        value = getattr(exc, attr, None)
        if value is not None:
            return str(value)
    return str(exc)


def _leader_error_is_agreeable(leaders_res, judge) -> bool:
    """Decide whether to agree with a leader that failed. Deterministic
    failures (a bad argument, a 4xx) must match exactly. A transient
    failure only has to be transient on both sides. An LLM failure
    always disagrees, so consensus rotates rather than locking bad
    state."""
    leader_msg = _error_text(leaders_res)
    try:
        judge()
    except gl.vm.UserError as exc:
        validator_msg = _error_text(exc)
        if validator_msg.startswith(ERR_EXPECTED) or validator_msg.startswith(ERR_EXTERNAL):
            return validator_msg == leader_msg
        if validator_msg.startswith(ERR_TRANSIENT) and leader_msg.startswith(ERR_TRANSIENT):
            return True
        return False
    except Exception:
        return False
    # The leader failed where this validator succeeded: disagree.
    return False


def _validate_scores(answer, loaded: list) -> list:
    """Check the model's per-source scores; return them in source order.

    Every source that loaded must have exactly one entry, with the URL
    copied exactly, both goals non-negative integers, and the status
    one of finished / not_finished / unknown. Entries for sources that
    did not load are dropped — that is either a hallucination or an
    injected URL, and neither may be stored. Any deviation is an
    [LLM_ERROR]: a model that cannot report scores in the required
    shape is not a model whose numbers can be stored.
    """
    if not isinstance(answer, dict):
        _fail(ERR_LLM, "model answer was not a JSON object")
    raw = answer.get("scores")
    if not isinstance(raw, list):
        _fail(ERR_LLM, "model answer has no scores list")
    by_source = {}
    for entry in raw:
        if not isinstance(entry, dict):
            continue
        source = entry.get("source")
        home_goals = entry.get("home_goals")
        away_goals = entry.get("away_goals")
        status = entry.get("status")
        if not isinstance(source, str):
            continue
        if source not in loaded:
            continue
        if (
            not isinstance(home_goals, int)
            or isinstance(home_goals, bool)
            or home_goals < 0
        ):
            _fail(ERR_LLM, "model home goals is not a non-negative integer")
        if (
            not isinstance(away_goals, int)
            or isinstance(away_goals, bool)
            or away_goals < 0
        ):
            _fail(ERR_LLM, "model away goals is not a non-negative integer")
        if not isinstance(status, str):
            _fail(ERR_LLM, "model status must be finished, not_finished or unknown")
        status = status.strip().lower()
        if status not in ("finished", "not_finished", "unknown"):
            _fail(ERR_LLM, "model status must be finished, not_finished or unknown")
        by_source[source] = {
            "home_goals": home_goals,
            "away_goals": away_goals,
            # Normalize to the internal constants: FINISHED /
            # NOT_FINISHED / UNKNOWN.
            "status": status.upper(),
        }
    scores = []
    for url in loaded:
        if url not in by_source:
            _fail(ERR_LLM, "model answer has no score for a loaded source")
        scores.append(by_source[url])
    return scores


# --------------------------------------------------------------------------
# Consensus worker (module level: it takes plain values, touches no
# storage, and is shared by leader and validators, so both sides run
# the same code).
# --------------------------------------------------------------------------
def _judge(home_team: str, away_team: str, match_date: str, sources: list) -> dict:
    """Fetch the sources, ask the model for one score per source, and
    return the judgement: the agreed final score and how many sources
    loaded. Raises a prefixed gl.vm.UserError on failure."""
    loaded = []
    failed = u256(0)
    failed_client = u256(0)
    pages = []
    for url in sources:
        try:
            resp = gl.nondet.web.get(url)
        except Exception:
            failed = failed + u256(1)
            continue
        status = resp.status
        if status == 200:
            raw = resp.body
            if raw is None:
                text = ""
            elif isinstance(raw, bytes):
                text = raw.decode("utf-8", "ignore")
            else:
                text = str(raw)
            loaded.append(url)
            pages.append({"source": url, "text": text[:MAX_PAGE_CHARS]})
        elif 400 <= status < 500:
            failed = failed + u256(1)
            failed_client = failed_client + u256(1)
        else:
            failed = failed + u256(1)
    # A score from a single feed is not an attestation: at least two
    # independent sources must load, or the round fails instead of
    # storing a one-feed number.
    if len(loaded) < MIN_SOURCES:
        if failed_client == failed:
            _fail(ERR_EXTERNAL, "fewer than two result sources loaded, all with client errors")
        _fail(ERR_TRANSIENT, "fewer than two result sources loaded")
    prompt = (
        "You are extracting the final score of a sports match from "
        "the source pages below. Answer with JSON only, using exactly "
        "one key:\n"
        '- "scores": a list of objects with keys "source", "home_goals", '
        '"away_goals" and "status"\n\n'
        "Rules:\n"
        '- "source" must be one of the URLs listed below, copied exactly.\n'
        '- "home_goals" and "away_goals" are the final goals of the '
        "home team and the away team, as non-negative integers. Never "
        "use a decimal point, never use a string.\n"
        '- "status" is "finished" only when the page shows a final '
        "score for THIS match (the teams and date below). Use "
        '"not_finished" when the page shows the match is scheduled, '
        "live, postponed, or has no final score yet. Use \"unknown\" "
        "when the page does not show this match at all.\n"
        "- Give exactly one entry for every source page listed below.\n"
        "- Read the score from the page itself. Do not use outside "
        "knowledge, do not guess, and do not report a half-time score "
        "or an aggregate across two legs as the final score."
    )
    prompt += "\n\nMatch: " + home_team + " vs " + away_team + " on " + match_date
    prompt += "\n\nSources:\n" + json.dumps(pages)
    try:
        answer = gl.nondet.exec_prompt(prompt, response_format="json")
    except Exception as exc:
        _fail(ERR_LLM, "model call failed: " + str(exc))
    scores = _validate_scores(answer, loaded)
    statuses = [entry["status"] for entry in scores]
    if all(status == MATCH_FINISHED for status in statuses):
        first_home = scores[0]["home_goals"]
        first_away = scores[0]["away_goals"]
        for entry in scores[1:]:
            if entry["home_goals"] != first_home or entry["away_goals"] != first_away:
                _fail(ERR_LLM, "sources disagree on the final score")
        return {
            "home_goals": u256(first_home),
            "away_goals": u256(first_away),
            "status": MATCH_FINISHED,
            "sources_loaded": u256(len(loaded)),
            "sources_failed": failed,
        }
    if all(status == MATCH_NOT_FINISHED for status in statuses):
        _fail(ERR_EXPECTED, "match has not finished yet")
    if all(status == MATCH_UNKNOWN for status in statuses):
        _fail(ERR_EXPECTED, "sources do not report this match")
    _fail(ERR_LLM, "sources disagree on the match status or score")


def _request_to_dict(req: ResultRequest) -> dict:
    return {
        "request_id": req.request_id,
        "home_team": req.home_team,
        "away_team": req.away_team,
        "match_date": req.match_date,
        "match_key": req.match_key,
        "sources": req.sources.split("\n") if req.sources != "" else [],
        "requester": req.requester.as_hex,
        "requested_at": req.requested_at,
        "status": req.status,
        "found": True,
    }


def _attestation_to_dict(att: ResultAttestation) -> dict:
    return {
        "attestation_id": att.attestation_id,
        "request_id": att.request_id,
        "home_team": att.home_team,
        "away_team": att.away_team,
        "match_date": att.match_date,
        "match_key": att.match_key,
        "home_goals": att.home_goals,
        "away_goals": att.away_goals,
        "winner": att.winner,
        "sources": att.sources.split("\n") if att.sources != "" else [],
        "verified_by": att.verified_by.as_hex,
        "verified_at": att.verified_at,
        "sources_loaded": att.sources_loaded,
        "sources_failed": att.sources_failed,
        "found": True,
    }


class SportsResultAttestation(gl.Contract):
    """Registry of consensus-attested final scores.

    Requests are made once per attestation; a match can be requested
    again at any time to add a newer record. Attestations are never
    deleted: an old attestation stays readable as history, and
    get_latest_attestation always returns the newest one for the
    match.
    """

    requests: TreeMap[u256, ResultRequest]
    attestations: TreeMap[u256, ResultAttestation]
    request_ids: DynArray[u256]
    attestation_ids: DynArray[u256]
    request_counter: u256
    attestation_counter: u256

    def __init__(self):
        self.request_counter = u256(0)
        self.attestation_counter = u256(0)

    # -- consensus ------------------------------------------------------
    # One round with gl.vm.run_nondet_unsafe. The leader and each
    # validator re-run _judge and their scores must match exactly:
    # same status (FINISHED) and same goal counts on both sides. The
    # goals are parsed integers compared by plain equality in
    # deterministic code, never by strict_eq on model output: a
    # byte-equality check on extracted scores would fail whenever two
    # validators read the same live score seconds apart, and a final
    # score is exact by nature, so there is no tolerance band to hide
    # behind — either both sides read the same final score, or nothing
    # is stored.
    def _attest_with_consensus(self, home_team: str, away_team: str, match_date: str, sources: list) -> dict:
        def leader_fn() -> dict:
            return _judge(home_team, away_team, match_date, sources)

        def validator_fn(leaders_res) -> bool:
            if not isinstance(leaders_res, gl.vm.Return):
                return _leader_error_is_agreeable(
                    leaders_res, lambda: _judge(home_team, away_team, match_date, sources)
                )
            try:
                mine = _judge(home_team, away_team, match_date, sources)
            except Exception:
                return False
            leader = leaders_res.calldata
            if not isinstance(leader, dict):
                return False
            if leader.get("status") != MATCH_FINISHED:
                return False
            if mine["status"] != MATCH_FINISHED:
                return False
            leader_home = leader.get("home_goals")
            leader_away = leader.get("away_goals")
            if (
                not isinstance(leader_home, int)
                or isinstance(leader_home, bool)
                or leader_home < 0
            ):
                return False
            if (
                not isinstance(leader_away, int)
                or isinstance(leader_away, bool)
                or leader_away < 0
            ):
                return False
            if leader.get("sources_loaded", 0) < MIN_SOURCES:
                return False
            if mine["sources_loaded"] < MIN_SOURCES:
                return False
            return mine["home_goals"] == leader_home and mine["away_goals"] == leader_away

        return gl.vm.run_nondet_unsafe(leader_fn, validator_fn)

    # -- public writes --------------------------------------------------
    @gl.public.write
    def request_result(self, home_team: str, away_team: str, match_date: str, sources: str) -> dict:
        """Request a final-score attestation for a match, from 2-3
        public result sources (comma-separated). Returns the new
        request id. Re-requesting a match is always allowed, even
        while an attestation exists."""
        clean_home = _clean_team(home_team, "home team")
        clean_away = _clean_team(away_team, "away team")
        clean_date = _clean_date(match_date)
        parsed = _parse_sources(sources)
        self.request_counter = self.request_counter + u256(1)
        request_id = self.request_counter
        self.requests[request_id] = ResultRequest(
            request_id=request_id,
            home_team=clean_home,
            away_team=clean_away,
            match_date=clean_date,
            match_key=_match_key(clean_home, clean_away, clean_date),
            sources="\n".join(parsed),
            requester=gl.message.sender_address,
            requested_at=_now_seconds(),
            status=STATUS_REQUESTED,
        )
        self.request_ids.append(request_id)
        return {"request_id": request_id, "status": STATUS_REQUESTED}

    @gl.public.write
    def attest_result(self, request_id: u256) -> dict:
        """Run validator consensus on a requested match and store the
        attestation. Anyone may call this: the caller decides nothing,
        consensus does. One request is attested once; a fresh record
        needs a new request."""
        request_id = _as_u256(request_id, "request id")
        if request_id not in self.requests:
            _fail(ERR_EXPECTED, "unknown request " + str(request_id))
        request = self.requests[request_id]
        if request.status != STATUS_REQUESTED:
            _fail(ERR_EXPECTED, "request " + str(request_id) + " is already " + request.status)
        # Storage cannot be read inside a nondet block: copy first,
        # then hand the consensus round plain in-memory values.
        request_mem = gl.storage.copy_to_memory(request)
        home_team = request_mem.home_team
        away_team = request_mem.away_team
        match_date = request_mem.match_date
        sources = request_mem.sources.split("\n")
        result = self._attest_with_consensus(home_team, away_team, match_date, sources)
        verified_at = _now_seconds()
        self.attestation_counter = self.attestation_counter + u256(1)
        attestation_id = self.attestation_counter
        self.attestations[attestation_id] = ResultAttestation(
            attestation_id=attestation_id,
            request_id=request_id,
            home_team=home_team,
            away_team=away_team,
            match_date=match_date,
            match_key=request_mem.match_key,
            home_goals=u256(int(result["home_goals"])),
            away_goals=u256(int(result["away_goals"])),
            winner=_derive_winner(u256(int(result["home_goals"])), u256(int(result["away_goals"]))),
            sources="\n".join(sources),
            verified_by=gl.message.sender_address,
            verified_at=verified_at,
            sources_loaded=u256(int(result["sources_loaded"])),
            sources_failed=u256(int(result["sources_failed"])),
        )
        self.attestation_ids.append(attestation_id)
        request.status = STATUS_ATTESTED
        self.requests[request_id] = request
        return {
            "attestation_id": attestation_id,
            "request_id": request_id,
            "home_team": home_team,
            "away_team": away_team,
            "match_date": match_date,
            "home_goals": int(result["home_goals"]),
            "away_goals": int(result["away_goals"]),
            "winner": _derive_winner(u256(int(result["home_goals"])), u256(int(result["away_goals"]))),
            "status": STATUS_ATTESTED,
        }

    # -- public views ---------------------------------------------------
    @gl.public.view
    def get_request(self, request_id: u256) -> dict:
        request_id = _as_u256(request_id, "request id")
        if request_id not in self.requests:
            _fail(ERR_EXPECTED, "unknown request " + str(request_id))
        return _request_to_dict(self.requests[request_id])

    @gl.public.view
    def get_attestation(self, attestation_id: u256) -> dict:
        attestation_id = _as_u256(attestation_id, "attestation id")
        if attestation_id not in self.attestations:
            _fail(ERR_EXPECTED, "unknown attestation " + str(attestation_id))
        return _attestation_to_dict(self.attestations[attestation_id])

    @gl.public.view
    def get_latest_attestation(self, home_team: str, away_team: str, match_date: str) -> dict:
        """The attestation a reader should trust for a match: the
        newest one. Raises when the match has none yet. Attestation
        ids grow with time, so scanning backwards finds the latest
        attestation for the match."""
        clean_home = _clean_team(home_team, "home team")
        clean_away = _clean_team(away_team, "away team")
        clean_date = _clean_date(match_date)
        key = _match_key(clean_home, clean_away, clean_date)
        index = len(self.attestation_ids) - 1
        while index >= 0:
            attestation = self.attestations[self.attestation_ids[index]]
            if attestation.match_key == key:
                return _attestation_to_dict(attestation)
            index = index - 1
        _fail(ERR_EXPECTED, "match " + key + " has no attestation yet")

    @gl.public.view
    def list_requests(self) -> dict:
        return {"requests": [_request_to_dict(self.requests[rid]) for rid in self.request_ids]}

    @gl.public.view
    def list_attestations(self) -> dict:
        return {
            "attestations": [
                _attestation_to_dict(self.attestations[aid]) for aid in self.attestation_ids
            ]
        }

    @gl.public.view
    def stats(self) -> dict:
        return {
            "requests": u256(len(self.request_ids)),
            "attestations": u256(len(self.attestation_ids)),
        }
