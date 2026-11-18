"""`N8nGmailMetadataQuery`'s parsing — the headers-only shape, measured on the live relay
2026-11-03 (two job-alert mails, 1.6 KB for both)."""

from reconciler.adapters.gmail import metadata_time, normalise_metadata

LIVE_SHAPE = {
    "id": "1a1a0b8dd089e94b",
    "threadId": "1a1a0b8dd089e94b",
    "snippet": "Jobgether Lead Tech SRE: This position is listed on behalf of a partner",
    "sizeEstimate": 112349,
    "internalDate": "1793697831000",
    "From": "Example Job Alerts <jobalerts-noreply@jobs.example>",
    "Subject": "Lead Tech SRE at Jobgether",
}


def test_the_address_is_parsed_out_of_the_from_header():
    m = normalise_metadata(LIVE_SHAPE)
    assert m["from"] == "jobalerts-noreply@jobs.example"
    assert m["from_name"] == "Example Job Alerts"
    assert m["from_domain"] == "jobs.example"
    assert m["upstream_id"] == "1a1a0b8dd089e94b" and m["subject"] == "Lead Tech SRE at Jobgether"


def test_a_bare_address_and_a_missing_header_both_parse():
    assert normalise_metadata({"From": "noreply@news.example"})["from"] == "noreply@news.example"
    assert normalise_metadata({})["from"] == ""


def test_a_display_name_that_looks_like_an_address_is_not_the_address():
    """The exact-sender guard compares `from`; a spoofed display name must not reach it."""
    m = normalise_metadata({"From": '"newsletter@news.example" <someone@else.com>'})
    assert m["from"] == "someone@else.com"


def test_time_is_gmails_internal_date_in_seconds():
    assert metadata_time(LIVE_SHAPE) == 1793697831
    assert metadata_time({}) == 0 and metadata_time({"internalDate": "garbage"}) == 0
