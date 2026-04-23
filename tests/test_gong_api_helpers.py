"""Tests for pure helpers in gong-sync/gong_api.py.

Covers:
  * get_account_info_from_call fallback chain: CRM Account -> external
    company -> email domain -> generic-domain filter -> (None, None)
  * format_transcript: empty input, speaker-id mapping, MM:SS timestamps
"""


def test_get_account_info_prefers_crm_account(gong_api):
    details = {
        "context": [{
            "objects": [{
                "objectType": "Account",
                "objectId": "0015000001A2bcDE",
                "name": "Acme Corp",
            }],
        }],
        "parties": [{
            "affiliation": "External",
            "company": "ShouldNotWin",
            "emailAddress": "noone@shouldnotwin.com",
        }],
    }
    assert gong_api.get_account_info_from_call(details) == ("0015000001A2bcDE", "Acme Corp")


def test_get_account_info_falls_back_to_external_company(gong_api):
    details = {
        "parties": [
            {"affiliation": "Internal", "company": "us", "emailAddress": "me@us.com"},
            {"affiliation": "External", "company": "Acme", "emailAddress": "ext@acme.com"},
        ],
    }
    assert gong_api.get_account_info_from_call(details) == ("Acme", "Acme")


def test_get_account_info_falls_back_to_email_domain(gong_api):
    details = {
        "parties": [
            {"affiliation": "External", "company": "", "emailAddress": "ext@acme.com"},
        ],
    }
    assert gong_api.get_account_info_from_call(details) == ("acme.com", "acme.com")


def test_get_account_info_skips_generic_email_domains(gong_api):
    details = {
        "parties": [
            {"affiliation": "External", "company": "", "emailAddress": "someone@gmail.com"},
        ],
    }
    assert gong_api.get_account_info_from_call(details) == (None, None)


def test_get_account_info_returns_none_when_no_external_party(gong_api):
    details = {"parties": [{"affiliation": "Internal", "company": "us"}]}
    assert gong_api.get_account_info_from_call(details) == (None, None)


def test_format_transcript_empty(gong_api):
    assert gong_api.format_transcript([], {}) == "No transcript available."
    assert gong_api.format_transcript(None, {}) == "No transcript available."


def test_format_transcript_maps_speakers_and_timestamps(gong_api):
    entries = [
        {
            "speakerId": "s1",
            "start": 0,
            "sentences": [{"text": "Hello there."}, {"text": "How are you?"}],
        },
        {
            "speakerId": "s2",
            "start": 125_000,  # 2 min 5 sec
            "sentences": [{"text": "Fine, thanks."}],
        },
        {
            "speakerId": "s_unknown",
            "start": 61_500,  # 1 min 1.5 sec -> 01:01
            "sentences": [{"text": "Anon."}],
        },
    ]
    participants = {"s1": {"name": "Alice"}, "s2": {"name": "Bob"}}
    out = gong_api.format_transcript(entries, participants)

    assert "[00:00] Alice: Hello there. How are you?" in out
    assert "[02:05] Bob: Fine, thanks." in out
    assert "[01:01] Unknown: Anon." in out
