from datetime import datetime

import pytest

from app.ingestion.cleaning import (
    DupCandidate,
    Rejection,
    clean_batch,
    clean_record,
    flag_near_duplicates,
    parse_datetime,
)
from app.reference import resolve_location, resolve_species

NOW = datetime(2026, 10, 28, 12, 0, 0)


def good(**over):
    rec = {
        "id": "abc",
        "date": "2022-08-01T06:40:31",
        "location": "Manchester",
        "species": "Marsh tick",
        "latinName": "Ixodes apronophorus",
    }
    rec.update(over)
    return rec


@pytest.mark.parametrize(
    "raw, expected",
    [
        ("2022-08-01T06:40:31", datetime(2022, 8, 1, 6, 40, 31)),
        ("2022-08-01T06:40:31Z", datetime(2022, 8, 1, 6, 40, 31)),
        ("2022-08-01T07:40:31+01:00", datetime(2022, 8, 1, 6, 40, 31)),
        ("2022-08-01 06:40:31", datetime(2022, 8, 1, 6, 40, 31)),
        ("2022-08-01T06:40:31.123456", datetime(2022, 8, 1, 6, 40, 31)),
        ("2022-08-01", datetime(2022, 8, 1)),
        ("01/08/2022", datetime(2022, 8, 1)),  # UK day-first
        ("01/08/2022 06:40", datetime(2022, 8, 1, 6, 40)),
        (1659335231, datetime(2022, 8, 1, 6, 27, 11)),  # epoch seconds
        (1659335231000, datetime(2022, 8, 1, 6, 27, 11)),  # epoch millis
        ("1659335231", datetime(2022, 8, 1, 6, 27, 11)),
    ],
)
def test_parse_datetime_accepts_common_shapes(raw, expected):
    assert parse_datetime(raw) == expected


@pytest.mark.parametrize("raw", [None, "", "  ", "not-a-date", "31/02/2021", "2021-13-45", True, "null"])
def test_parse_datetime_rejects_garbage(raw):
    assert parse_datetime(raw) is None


def test_clean_record_happy_path_enriches_geography():
    rec = clean_record(good(), now=NOW, min_year=1990)
    assert not isinstance(rec, Rejection)
    assert (rec.city, rec.region, rec.nation) == ("Manchester", "North West", "England")
    assert rec.latitude == pytest.approx(53.48, abs=0.01)
    assert rec.flags == []


@pytest.mark.parametrize(
    "override, reason",
    [
        ({"date": None}, "missing_date"),
        ({"date": ""}, "missing_date"),
        ({"date": "garbage"}, "invalid_date"),
        ({"date": "2099-01-01T00:00:00"}, "future_date"),
        ({"date": "1850-01-01T00:00:00"}, "implausible_date"),
        ({"location": None}, "missing_location"),
        ({"location": "   "}, "missing_location"),
        ({"location": "N/A"}, "missing_location"),
    ],
)
def test_clean_record_rejections(override, reason):
    result = clean_record(good(**override), now=NOW, min_year=1990)
    assert isinstance(result, Rejection)
    assert reason in result.reasons


def test_non_object_is_rejected():
    assert clean_record("garbage", now=NOW, min_year=1990).reasons == ["not_an_object"]


@pytest.mark.parametrize(
    "location, city, flag",
    [
        ("  MANCHESTER ", "Manchester", None),
        ("manchester", "Manchester", None),
        ("Manchster", "Manchester", "location_fuzzy_matched"),
        ("Newcastle upon Tyne", "Newcastle", None),
        ("Greater London", "London", None),
        ("Little Snoring", "Little Snoring", "location_unmapped"),
    ],
)
def test_location_normalisation(location, city, flag):
    rec = clean_record(good(location=location), now=NOW, min_year=1990)
    assert rec.city == city
    if flag:
        assert flag in rec.flags
    else:
        assert not [f for f in rec.flags if f.startswith("location")]


def test_unmapped_location_kept_without_region():
    rec = clean_record(good(location="Wetwang"), now=NOW, min_year=1990)
    assert rec.region == "Unmapped" and rec.latitude is None


def test_species_inferred_from_latin_and_vice_versa():
    a = clean_record(good(species=None), now=NOW, min_year=1990)
    assert a.species == "Marsh tick" and "species_inferred_from_latin" in a.flags
    b = clean_record(good(latinName=""), now=NOW, min_year=1990)
    assert b.latin_name == "Ixodes apronophorus" and "latin_inferred_from_species" in b.flags


def test_conflicting_species_prefers_latin():
    rec = clean_record(good(species="Marsh tick", latinName="Ixodes canisuga"), now=NOW, min_year=1990)
    assert rec.species == "Fox/badger tick"
    assert "species_conflict_latin_preferred" in rec.flags


def test_missing_species_kept_as_unidentified():
    rec = clean_record(good(species=None, latinName=None), now=NOW, min_year=1990)
    assert rec.species == "Unidentified" and "species_missing" in rec.flags


def test_alternative_field_names_are_understood():
    rec = clean_record(
        {"sighting_id": "x1", "Date": "01/08/2022", "City": "leeds", "common_name": "Tree-hole tick"},
        now=NOW,
        min_year=1990,
    )
    assert (rec.id, rec.city, rec.species, rec.latin_name) == (
        "x1",
        "Leeds",
        "Tree-hole tick",
        "Ixodes arboricola",
    )


def test_missing_id_gets_deterministic_id():
    a = clean_record(good(id=None), now=NOW, min_year=1990)
    b = clean_record(good(id=None), now=NOW, min_year=1990)
    assert a.id == b.id and a.id.startswith("gen_") and "id_generated" in a.flags


def test_batch_keeps_first_copy_of_duplicate_ids_unless_later_is_more_complete():
    items = [good(location="Manchster"), good(), good(location="Leeds")]
    result = clean_batch(items, now=NOW)
    assert result.accepted == 1
    assert result.records[0].city == "Manchester"
    assert result.records[0].flags == []  # the clean second copy replaced the typo'd first copy
    assert result.stats["duplicate_id_in_batch"] == 2
    assert result.stats["duplicate_id_conflicting_content"] >= 1


def test_batch_counts_rejections_and_flags():
    items = [good(id="1"), good(id="2", date="bad"), "junk", good(id="3", location="Manchster")]
    result = clean_batch(items, now=NOW)
    assert result.accepted == 2 and len(result.rejections) == 2
    assert result.stats["rejected:invalid_date"] == 1
    assert result.stats["flag:location_fuzzy_matched"] == 1


def _dc(i, city="Leeds", species="Marsh tick", second=0):
    return DupCandidate(f"id{i}", city, species, datetime(2022, 1, 1, 12, 0, 0).replace(second=second))


def test_near_duplicates_flag_later_reports_within_window():
    rows = [_dc(1, second=0), _dc(2, second=30), _dc(3, city="York", second=30), _dc(4, species="Sheep tick")]
    result = flag_near_duplicates(rows, window_s=300)
    assert result == {"id1": None, "id2": "id1", "id3": None, "id4": None}


def test_near_duplicates_respect_window():
    a = DupCandidate("a", "Leeds", "Marsh tick", datetime(2022, 1, 1, 12, 0))
    b = DupCandidate("b", "Leeds", "Marsh tick", datetime(2022, 1, 1, 12, 10))
    assert flag_near_duplicates([a, b], 300) == {"a": None, "b": None}
    assert flag_near_duplicates([a, b], 900) == {"a": None, "b": "a"}


def test_reference_resolution_helpers():
    assert resolve_location("nottingam").name == "Nottingham"
    sp, flags = resolve_species("fox badger tick", None)
    assert sp.common_name == "Fox/badger tick" and "latin_inferred_from_species" in flags


def test_near_duplicate_bursts_collapse_onto_first_report():
    t0 = datetime(2022, 1, 1, 12, 0)
    from datetime import timedelta

    rows = [
        DupCandidate(x, "Leeds", "Marsh tick", t0 + timedelta(minutes=m))
        for x, m in (("a", 0), ("b", 4), ("c", 8))
    ]
    assert flag_near_duplicates(rows, 300) == {"a": None, "b": "a", "c": "a"}
