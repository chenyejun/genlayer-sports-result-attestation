"""Direct tests for SportsResultAttestation — mocked web + LLM, no network.

Every consensus round runs against mocks: result pages are stubbed
with mock_web, model answers with mock_llm. run_validator() replays the
captured validator with the current mocks, so swapping the mocks
between the leader run and the replay simulates a validator that sees
different external data.
"""

import json
import re

from tests.direct.conftest import to_hex

URL_BBC = "https://www.bbc.com/sport/football/scores/2026-11-22"
URL_ESPN = "https://site.api.espn.com/apis/site/v2/sports/soccer/eng.1/scoreboard?dates=20261122"
URL_FLASH = "https://www.flashscore.com/match/arsenal-chelsea-2026-11-22/"

SOURCES_3 = URL_BBC + "," + URL_ESPN + "," + URL_FLASH
SOURCES_2 = URL_BBC + "," + URL_ESPN

HOME = "Arsenal"
AWAY = "Chelsea"
DATE = "2026-11-22"

PAGE_BBC = "Arsenal 2 - 1 Chelsea, Full Time, Premier League, 22 November 2026"
PAGE_ESPN = '{"events": [{"name": "Arsenal vs Chelsea", "status": "FT", "homeScore": 2, "awayScore": 1}]}'
PAGE_FLASH = "Arsenal - Chelsea 2:1, Finished"


def _scores_answer(home, away, status="finished"):
    """Model answer: one score per source, in source order."""
    return {
        "scores": [
            {"source": URL_BBC, "home_goals": home, "away_goals": away, "status": status},
            {"source": URL_ESPN, "home_goals": home, "away_goals": away, "status": status},
            {"source": URL_FLASH, "home_goals": home, "away_goals": away, "status": status},
        ]
    }


def _mock_pages(vm, pages):
    for url, body in pages.items():
        vm.mock_web(re.escape(url), {"status": 200, "body": body})


def _mock_page_status(vm, url, status):
    vm.mock_web(re.escape(url), {"status": status, "body": ""})


def _mock_llm(vm, answer):
    vm.mock_llm("home_goals", json.dumps(answer))


def _request(vm, contract, home=HOME, away=AWAY, date=DATE, sources=SOURCES_3):
    return contract.request_result(home, away, date, sources)


def _attest_3_sources(vm, contract, request_id=1, home=2, away=1):
    _mock_pages(vm, {URL_BBC: PAGE_BBC, URL_ESPN: PAGE_ESPN, URL_FLASH: PAGE_FLASH})
    _mock_llm(vm, _scores_answer(home, away))
    return contract.attest_result(request_id)


# -- requests ------------------------------------------------------------


def test_request_stores_request(direct_vm, direct_deploy, direct_alice):
    contract = direct_deploy("contracts/SportsResultAttestation.py")
    direct_vm.sender = direct_alice

    result = _request(direct_vm, contract)

    assert result["request_id"] == 1
    assert result["status"] == "REQUESTED"

    req = contract.get_request(1)
    assert req["home_team"] == HOME
    assert req["away_team"] == AWAY
    assert req["match_date"] == DATE
    assert req["match_key"] == "ARSENAL vs CHELSEA 2026-11-22"
    assert req["sources"] == [URL_BBC, URL_ESPN, URL_FLASH]
    assert req["requester"] == to_hex(direct_alice)
    assert req["requested_at"] > 0
    assert req["status"] == "REQUESTED"


def test_request_normalizes_team_whitespace(direct_vm, direct_deploy):
    contract = direct_deploy("contracts/SportsResultAttestation.py")

    result = _request(direct_vm, contract, home="  Arsenal   FC ", away="chelsea")

    assert result["request_id"] == 1
    req = contract.get_request(1)
    assert req["home_team"] == "Arsenal FC"
    assert req["match_key"] == "ARSENAL FC vs CHELSEA 2026-11-22"


def test_request_rejects_bad_dates(direct_vm, direct_deploy):
    contract = direct_deploy("contracts/SportsResultAttestation.py")

    with direct_vm.expect_revert("[EXPECTED]"):
        _request(direct_vm, contract, date="22/11/2026")
    with direct_vm.expect_revert("[EXPECTED]"):
        _request(direct_vm, contract, date="2026-13-01")
    with direct_vm.expect_revert("[EXPECTED]"):
        _request(direct_vm, contract, date="2026-11-32")
    with direct_vm.expect_revert("[EXPECTED]"):
        _request(direct_vm, contract, date="2026-11-2")


def test_request_rejects_bad_teams_and_sources(direct_vm, direct_deploy):
    contract = direct_deploy("contracts/SportsResultAttestation.py")

    with direct_vm.expect_revert("[EXPECTED]"):
        _request(direct_vm, contract, home="   ")
    with direct_vm.expect_revert("[EXPECTED]"):
        _request(direct_vm, contract, home="A" * 61)
    with direct_vm.expect_revert("[EXPECTED]"):
        _request(direct_vm, contract, sources=URL_BBC)
    with direct_vm.expect_revert("[EXPECTED]"):
        _request(
            direct_vm,
            contract,
            sources=SOURCES_3 + "," + "https://example.com/scores",
        )
    with direct_vm.expect_revert("[EXPECTED]"):
        _request(direct_vm, contract, sources=URL_BBC + "," + URL_BBC)
    with direct_vm.expect_revert("[EXPECTED]"):
        _request(
            direct_vm,
            contract,
            sources="http://www.bbc.com/sport/football/scores" + "," + URL_ESPN,
        )


def test_re_requesting_a_match_is_allowed(direct_vm, direct_deploy):
    contract = direct_deploy("contracts/SportsResultAttestation.py")

    first = _request(direct_vm, contract)
    second = _request(direct_vm, contract)

    assert first["request_id"] == 1
    assert second["request_id"] == 2
    assert len(contract.list_requests()["requests"]) == 2


# -- attestation ---------------------------------------------------------


def test_attest_stores_score_and_winner(direct_vm, direct_deploy, direct_alice):
    contract = direct_deploy("contracts/SportsResultAttestation.py")
    direct_vm.sender = direct_alice
    direct_vm.warp("2026-11-23T00:00:00Z")
    _request(direct_vm, contract)

    result = _attest_3_sources(direct_vm, contract)

    assert result["attestation_id"] == 1
    assert result["request_id"] == 1
    assert result["home_team"] == HOME
    assert result["away_team"] == AWAY
    assert result["home_goals"] == 2
    assert result["away_goals"] == 1
    assert result["winner"] == "HOME"
    assert result["status"] == "ATTESTED"

    att = contract.get_attestation(1)
    assert att["home_goals"] == 2
    assert att["away_goals"] == 1
    assert att["winner"] == "HOME"
    assert att["sources"] == [URL_BBC, URL_ESPN, URL_FLASH]
    assert att["sources_loaded"] == 3
    assert att["sources_failed"] == 0
    assert att["verified_by"] == to_hex(direct_alice)
    assert att["verified_at"] > 0

    assert contract.get_request(1)["status"] == "ATTESTED"
    assert contract.get_latest_attestation(HOME, AWAY, DATE)["attestation_id"] == 1


def test_attest_draw_winner(direct_vm, direct_deploy):
    contract = direct_deploy("contracts/SportsResultAttestation.py")
    _request(direct_vm, contract, home="Liverpool", away="Manchester City", date="2026-11-21")

    result = _attest_3_sources(direct_vm, contract, home=1, away=1)

    assert result["home_goals"] == 1
    assert result["away_goals"] == 1
    assert result["winner"] == "DRAW"


def test_attest_away_winner(direct_vm, direct_deploy):
    contract = direct_deploy("contracts/SportsResultAttestation.py")
    _request(direct_vm, contract)

    result = _attest_3_sources(direct_vm, contract, home=0, away=3)

    assert result["winner"] == "AWAY"


def test_validator_agrees_with_leader(direct_vm, direct_deploy):
    contract = direct_deploy("contracts/SportsResultAttestation.py")
    _request(direct_vm, contract)
    _attest_3_sources(direct_vm, contract)

    # Replays the captured validator with the same mocks.
    assert direct_vm.run_validator() is True


def test_validator_disagrees_on_score(direct_vm, direct_deploy):
    contract = direct_deploy("contracts/SportsResultAttestation.py")
    _request(direct_vm, contract)
    _attest_3_sources(direct_vm, contract)

    # The validator extracts a different final score: 3-1.
    direct_vm.clear_mocks()
    _mock_pages(direct_vm, {URL_BBC: PAGE_BBC, URL_ESPN: PAGE_ESPN, URL_FLASH: PAGE_FLASH})
    _mock_llm(direct_vm, _scores_answer(3, 1))

    assert direct_vm.run_validator() is False


def test_validator_disagrees_when_match_not_finished(direct_vm, direct_deploy):
    contract = direct_deploy("contracts/SportsResultAttestation.py")
    _request(direct_vm, contract)
    _attest_3_sources(direct_vm, contract)

    # The validator sees the match as still live: no agreement.
    direct_vm.clear_mocks()
    _mock_pages(direct_vm, {URL_BBC: PAGE_BBC, URL_ESPN: PAGE_ESPN, URL_FLASH: PAGE_FLASH})
    _mock_llm(direct_vm, _scores_answer(2, 1, status="not_finished"))

    assert direct_vm.run_validator() is False


def test_two_sources_loading_is_enough(direct_vm, direct_deploy):
    contract = direct_deploy("contracts/SportsResultAttestation.py")
    _request(direct_vm, contract)

    # Flashscore is down with a 404; the other two load.
    _mock_pages(direct_vm, {URL_BBC: PAGE_BBC, URL_ESPN: PAGE_ESPN})
    _mock_page_status(direct_vm, URL_FLASH, 404)
    _mock_llm(
        direct_vm,
        {
            "scores": [
                {"source": URL_BBC, "home_goals": 2, "away_goals": 1, "status": "finished"},
                {"source": URL_ESPN, "home_goals": 2, "away_goals": 1, "status": "finished"},
            ]
        },
    )
    result = contract.attest_result(1)

    assert result["home_goals"] == 2
    assert result["away_goals"] == 1
    att = contract.get_attestation(1)
    assert att["sources_loaded"] == 2
    assert att["sources_failed"] == 1


def test_fewer_than_two_sources_loading_fails(direct_vm, direct_deploy):
    contract = direct_deploy("contracts/SportsResultAttestation.py")
    _request(direct_vm, contract)

    # Two sources fail with client errors: classified [EXTERNAL].
    _mock_pages(direct_vm, {URL_BBC: PAGE_BBC})
    _mock_page_status(direct_vm, URL_ESPN, 404)
    _mock_page_status(direct_vm, URL_FLASH, 404)
    _mock_llm(direct_vm, _scores_answer(2, 1))

    with direct_vm.expect_revert("[EXTERNAL]"):
        contract.attest_result(1)
    assert contract.get_request(1)["status"] == "REQUESTED"


def test_all_sources_server_error_is_transient(direct_vm, direct_deploy):
    contract = direct_deploy("contracts/SportsResultAttestation.py")
    _request(direct_vm, contract)

    _mock_page_status(direct_vm, URL_BBC, 500)
    _mock_page_status(direct_vm, URL_ESPN, 503)
    _mock_page_status(direct_vm, URL_FLASH, 429)

    with direct_vm.expect_revert("[TRANSIENT]"):
        contract.attest_result(1)


def test_match_not_finished_is_expected(direct_vm, direct_deploy):
    contract = direct_deploy("contracts/SportsResultAttestation.py")
    _request(direct_vm, contract)

    _mock_pages(direct_vm, {URL_BBC: PAGE_BBC, URL_ESPN: PAGE_ESPN, URL_FLASH: PAGE_FLASH})
    _mock_llm(direct_vm, _scores_answer(0, 0, status="not_finished"))

    with direct_vm.expect_revert("[EXPECTED]"):
        contract.attest_result(1)
    assert contract.get_request(1)["status"] == "REQUESTED"


def test_sources_not_reporting_match_is_expected(direct_vm, direct_deploy):
    contract = direct_deploy("contracts/SportsResultAttestation.py")
    _request(direct_vm, contract)

    _mock_pages(direct_vm, {URL_BBC: PAGE_BBC, URL_ESPN: PAGE_ESPN, URL_FLASH: PAGE_FLASH})
    _mock_llm(direct_vm, _scores_answer(0, 0, status="unknown"))

    with direct_vm.expect_revert("[EXPECTED]"):
        contract.attest_result(1)


def test_sources_disagreeing_on_score_is_llm_error(direct_vm, direct_deploy):
    contract = direct_deploy("contracts/SportsResultAttestation.py")
    _request(direct_vm, contract)

    _mock_pages(direct_vm, {URL_BBC: PAGE_BBC, URL_ESPN: PAGE_ESPN, URL_FLASH: PAGE_FLASH})
    _mock_llm(
        direct_vm,
        {
            "scores": [
                {"source": URL_BBC, "home_goals": 2, "away_goals": 1, "status": "finished"},
                {"source": URL_ESPN, "home_goals": 2, "away_goals": 1, "status": "finished"},
                {"source": URL_FLASH, "home_goals": 1, "away_goals": 1, "status": "finished"},
            ]
        },
    )

    with direct_vm.expect_revert("[LLM_ERROR]"):
        contract.attest_result(1)


def test_bad_model_answers_are_llm_errors(direct_vm, direct_deploy):
    contract = direct_deploy("contracts/SportsResultAttestation.py")
    _request(direct_vm, contract)

    # No scores list at all.
    _mock_pages(direct_vm, {URL_BBC: PAGE_BBC, URL_ESPN: PAGE_ESPN, URL_FLASH: PAGE_FLASH})
    _mock_llm(direct_vm, {"score": "2-1"})
    with direct_vm.expect_revert("[LLM_ERROR]"):
        contract.attest_result(1)

    # A loaded source is missing from the answer.
    direct_vm.clear_mocks()
    _mock_pages(direct_vm, {URL_BBC: PAGE_BBC, URL_ESPN: PAGE_ESPN, URL_FLASH: PAGE_FLASH})
    _mock_llm(
        direct_vm,
        {
            "scores": [
                {"source": URL_BBC, "home_goals": 2, "away_goals": 1, "status": "finished"},
                {"source": URL_ESPN, "home_goals": 2, "away_goals": 1, "status": "finished"},
            ]
        },
    )
    with direct_vm.expect_revert("[LLM_ERROR]"):
        contract.attest_result(1)

    # A goal is negative.
    direct_vm.clear_mocks()
    _mock_pages(direct_vm, {URL_BBC: PAGE_BBC, URL_ESPN: PAGE_ESPN, URL_FLASH: PAGE_FLASH})
    _mock_llm(direct_vm, _scores_answer(-1, 1))
    with direct_vm.expect_revert("[LLM_ERROR]"):
        contract.attest_result(1)

    # A status is not one of the three allowed values.
    direct_vm.clear_mocks()
    _mock_pages(direct_vm, {URL_BBC: PAGE_BBC, URL_ESPN: PAGE_ESPN, URL_FLASH: PAGE_FLASH})
    _mock_llm(
        direct_vm,
        {
            "scores": [
                {"source": URL_BBC, "home_goals": 2, "away_goals": 1, "status": "maybe"},
                {"source": URL_ESPN, "home_goals": 2, "away_goals": 1, "status": "finished"},
                {"source": URL_FLASH, "home_goals": 2, "away_goals": 1, "status": "finished"},
            ]
        },
    )
    with direct_vm.expect_revert("[LLM_ERROR]"):
        contract.attest_result(1)

    assert contract.get_request(1)["status"] == "REQUESTED"


def test_attest_twice_and_unknown_ids_rejected(direct_vm, direct_deploy):
    contract = direct_deploy("contracts/SportsResultAttestation.py")
    _request(direct_vm, contract)
    _attest_3_sources(direct_vm, contract)

    with direct_vm.expect_revert("[EXPECTED]"):
        contract.attest_result(1)
    with direct_vm.expect_revert("[EXPECTED]"):
        contract.attest_result(999)
    with direct_vm.expect_revert("[EXPECTED]"):
        contract.get_request(999)
    with direct_vm.expect_revert("[EXPECTED]"):
        contract.get_attestation(999)
    with direct_vm.expect_revert("[EXPECTED]"):
        contract.get_latest_attestation("Real Madrid", "Barcelona", "2026-11-22")


# -- views ---------------------------------------------------------------


def test_latest_attestation_is_newest_for_match(direct_vm, direct_deploy):
    contract = direct_deploy("contracts/SportsResultAttestation.py")
    _request(direct_vm, contract)
    _attest_3_sources(direct_vm, contract)
    _request(direct_vm, contract)
    _attest_3_sources(direct_vm, contract, request_id=2)

    latest = contract.get_latest_attestation(HOME, AWAY, DATE)
    assert latest["attestation_id"] == 2
    assert latest["request_id"] == 2

    # The first attestation is still readable as history.
    assert contract.get_attestation(1)["attestation_id"] == 1


def test_matches_are_independent(direct_vm, direct_deploy):
    contract = direct_deploy("contracts/SportsResultAttestation.py")
    _request(direct_vm, contract)
    _attest_3_sources(direct_vm, contract)
    _request(direct_vm, contract, home="Liverpool", away="Manchester City", date="2026-11-21")
    _attest_3_sources(direct_vm, contract, request_id=2, home=1, away=1)

    assert contract.get_latest_attestation(HOME, AWAY, DATE)["attestation_id"] == 1
    assert contract.get_latest_attestation("Liverpool", "Manchester City", "2026-11-21")["attestation_id"] == 2


def test_list_and_stats(direct_vm, direct_deploy):
    contract = direct_deploy("contracts/SportsResultAttestation.py")
    _request(direct_vm, contract)
    _attest_3_sources(direct_vm, contract)
    _request(direct_vm, contract, home="Liverpool", away="Manchester City", date="2026-11-21")

    assert len(contract.list_requests()["requests"]) == 2
    assert len(contract.list_attestations()["attestations"]) == 1
    stats = contract.stats()
    assert stats["requests"] == 2
    assert stats["attestations"] == 1
