"""Composition (ADR 0012).

`app` is where a run gets wired; `run.py` is a thin skin over it. What is worth pinning here is
the small amount of *logic* that lives at that seam — which sequence number a new cycle takes,
and which period a scheduled open covers.
"""

import pathlib
from datetime import date

import pytest

from reconciler import app
from reconciler.core import Cycle
from reconciler.definition import load_definitions

ROOT = pathlib.Path(__file__).resolve().parents[1]
PROCESSES = ROOT / "tests" / "fixtures" / "processes"


def _definition(name="monthly_invoice"):
    return next(d for d in load_definitions(PROCESSES) if d.name == name)


def _cycle(key):
    return Cycle(key=key, process="monthly_invoice", user_id="demo", opened_at=0, context={})


# --- the sequence a new cycle takes ------------------------------------------------------------

def test_the_sequence_is_read_back_out_of_the_key():
    """Generated and compared rather than parsed: the same `str.format` that minted the key
    reads it back, so a change to the `{seq:03d}` spec cannot desynchronise the two."""
    definition = _definition()
    assert app.sequence_of(definition, "acme_invoice_2026_008", 2026) == 8
    assert app.sequence_of(definition, "acme_invoice_2026_008", 2025) is None   # wrong year
    assert app.sequence_of(definition, "something_else", 2026) is None


def test_the_next_sequence_is_one_past_the_highest_used_this_year():
    definition = _definition()
    cycles = [_cycle("acme_invoice_2026_007"), _cycle("acme_invoice_2026_008")]
    assert app.next_sequence(definition, cycles, 2026) == 9


def test_a_year_with_no_cycles_starts_at_one():
    assert app.next_sequence(_definition(), [], 2027) == 1


def test_last_year_s_cycles_do_not_advance_this_year_s_sequence():
    """The sequence is PER YEAR (the invoice number is not the month — that mistake is what
    the old design's padding bug came from), so January must restart at 1."""
    definition = _definition()
    cycles = [_cycle("acme_invoice_2026_011"), _cycle("acme_invoice_2027_001")]
    assert app.next_sequence(definition, cycles, 2027) == 2
    assert app.next_sequence(definition, cycles, 2028) == 1


def test_a_gap_left_by_a_hand_opened_cycle_is_not_reused():
    """One past the HIGHEST, not the first free slot. A number already sent to a client must
    never be minted again, and a gap is cheaper than a collision."""
    definition = _definition()
    cycles = [_cycle("acme_invoice_2026_001"), _cycle("acme_invoice_2026_005")]
    assert app.next_sequence(definition, cycles, 2026) == 6


# --- the period a scheduled open covers --------------------------------------------------------

@pytest.mark.parametrize("today,expected", [
    (date(2026, 9, 1), {"start": "2026-08-01", "end": "2026-08-31"}),
    (date(2026, 3, 1), {"start": "2026-02-01", "end": "2026-02-28"}),   # short month
    (date(2028, 3, 1), {"start": "2028-02-01", "end": "2028-02-29"}),   # leap year
    (date(2027, 1, 1), {"start": "2026-12-01", "end": "2026-12-31"}),   # across the year
])
def test_a_cron_on_the_first_invoices_the_month_that_just_ended(today, expected):
    assert app.previous_month(today) == expected


def test_walking_a_fresh_cycle_needs_no_database(monkeypatch):
    """The preview path, and the reason `open_and_advance_flow` uses it.

    A cycle opened one line ago has nothing recorded to read — and a milestone reaches Neon
    ASYNCHRONOUSLY (gateway -> Kafka -> Vector -> persister), so looking it back up races the
    write. `advance` did exactly that and failed with `no cycle 'acme_invoice_2026_010'`; under
    dry-run it could never work, since a dry open writes nothing to find.

    So this asserts the property that makes the race impossible: walking a just-made cycle
    touches no database at all. `NEON_DATABASE_URL` is removed here, and a DB read would raise
    `ConfigurationError`.
    """
    for var in ("NEON_DATABASE_URL", "GMAIL_QUERY_URL", "LLM_RELAY_URL",
                "MAIL_RELAY_URL", "VECTOR_BASE_URL", "CRAFTMYPDF_API_KEY"):
        monkeypatch.delenv(var, raising=False)

    cycle = Cycle(key="acme_invoice_2026_099", process="monthly_invoice", user_id="demo",
                  opened_at=1788000000,
                  context={"invoice_number": 99,
                           "worked_period": {"start": "2026-08-01", "end": "2026-08-31"}})

    outcome = app.walk_fresh("monthly_invoice", cycle,
                             options=app.RunOptions(dry_run=True, processes_dir=PROCESSES))

    # It gets through the pure + notify stages and then stops at the first gate, because no
    # finder is wired without GMAIL_QUERY_URL. `None` is that loud stop, not a silent skip.
    assert outcome is None


def test_the_wiring_imports_with_no_third_party_packages_installed():
    """CI installs with `pip --no-deps`, so every module a test reaches must import against a
    bare interpreter. `app.py` pulls in every adapter, and `adapters/neon.py` had a
    module-level `import psycopg` — which collapsed the ENTIRE pytest collection in CI while
    passing locally, where psycopg is installed. Simulated here rather than discovered there.
    """
    import importlib
    import sys

    # Exactly what CI does NOT install: it has pytest, ruff, pydantic and pyyaml,
    # then `pip install -e . --no-deps`. Everything else must be imported lazily.
    blocked = {"psycopg", "pymysql", "dotenv", "prefect", "anthropic", "quixstreams"}
    saved = {k: v for k, v in sys.modules.items()
             if k.startswith("reconciler") or k.split(".")[0] in blocked}
    try:
        for name in list(sys.modules):
            if name.startswith("reconciler") or name.split(".")[0] in blocked:
                del sys.modules[name]
        for name in blocked:
            sys.modules[name] = None      # makes `import <name>` raise ImportError

        importlib.import_module("reconciler.app")
        importlib.import_module("reconciler.run")
    finally:
        for name in list(sys.modules):
            if name.startswith("reconciler") or name.split(".")[0] in blocked:
                del sys.modules[name]
        sys.modules.update(saved)
