from datetime import datetime, timezone

import pytest

from sociyx.filters import Filter, parse_bound
from sociyx.models import ContentItem


def item(ts, text="hello", ctype="post"):
    return ContentItem("fixture", "fixture:alice", "i", ctype, ts, text)


def utc(*a):
    return datetime(*a, tzinfo=timezone.utc)


def test_since_is_inclusive():
    f = Filter(since=parse_bound("2021-01-01", end=False))
    assert f.matches(item(utc(2021, 1, 1)))
    assert not f.matches(item(utc(2020, 12, 31, 23, 59, 59, 999999)))


def test_date_only_until_covers_whole_day_but_not_next():
    f = Filter(until=parse_bound("2021-12-31", end=True))
    assert f.matches(item(utc(2021, 12, 31, 23, 59, 59, 999999)))
    assert not f.matches(item(utc(2022, 1, 1, 0, 0, 0)))


def test_exact_boundary_instants_with_explicit_datetimes():
    f = Filter(since=parse_bound("2021-06-01T12:00:00Z", end=False), until=parse_bound("2021-06-01T12:00:00Z", end=True))
    assert f.matches(item(utc(2021, 6, 1, 12)))
    assert not f.matches(item(utc(2021, 6, 1, 12, 0, 0, 1)))
    assert not f.matches(item(utc(2021, 6, 1, 11, 59, 59, 999999)))


def test_since_after_until_is_an_error():
    with pytest.raises(ValueError):
        Filter(since=utc(2022, 1, 1), until=utc(2021, 1, 1))


def test_none_timestamps_excluded_only_with_date_bounds():
    undated = item(None)
    assert Filter().matches(undated)
    assert Filter(keywords=["hello"]).matches(undated)
    assert not Filter(since=utc(2000, 1, 1)).matches(undated)
    assert not Filter(until=utc(2100, 1, 1)).matches(undated)


def test_offset_input_converted_to_utc():
    b = parse_bound("2021-01-01T00:00:00+05:30", end=False)
    assert b == utc(2020, 12, 31, 18, 30)
    f = Filter(since=b)
    assert f.matches(item(utc(2020, 12, 31, 18, 30)))
    assert not f.matches(item(utc(2020, 12, 31, 18, 29, 59)))


def test_keywords_case_insensitive_and_casefolded():
    assert Filter(keywords=["vacation"]).matches(item(None, "Back from VACATION"))
    assert Filter(keywords=["CAFÉ"]).matches(item(None, "a café au lait"))
    assert Filter(keywords=["café"]).matches(item(None, "CAFÉ"))
    assert Filter(keywords=["straße"]).matches(item(None, "STRASSE" .replace("SS", "ß")))


def test_any_keyword_semantics():
    f = Filter(keywords=["alpha", "beta"])
    assert f.matches(item(None, "only beta here"))
    assert not f.matches(item(None, "gamma"))


def test_exclude_wins_over_include():
    f = Filter(keywords=["crypto"], exclude_keywords=["winter"])
    assert f.matches(item(None, "crypto summer"))
    assert not f.matches(item(None, "Crypto WINTER"))


def test_type_filter():
    f = Filter(content_types=["like"])
    assert f.matches(item(None, ctype="like"))
    assert not f.matches(item(None, ctype="post"))
