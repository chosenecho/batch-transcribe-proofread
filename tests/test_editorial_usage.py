"""Proxy usage fields retain unknowns and never overwrite explicit counters."""
from copy import deepcopy

import pytest

from bili_asr.deepseek import normalize_usage


def test_opencode_usage_reports_total_and_nested_cached_tokens_without_mutating_response():
    envelope = {"usage": {"prompt_tokens": 15373, "completion_tokens": 14215,
        "total_tokens": 29588, "prompt_tokens_details": {"cached_tokens": 0},
        "completion_tokens_details": {"reasoning_tokens": 12188}}}
    original = deepcopy(envelope)
    actual = normalize_usage(envelope)
    assert actual["total_tokens"] == 29588
    assert actual["cached_tokens"] == actual["prompt_cache_hit_tokens"] == 0
    assert actual["prompt_cache_miss_tokens"] is None
    assert actual["reasoning_tokens"] == 12188
    assert envelope == original


@pytest.mark.parametrize("value", [-1, True, "3", 1.5, None, {}])
def test_bad_optional_usage_values_stay_unknown(value):
    actual = normalize_usage({"usage": {"total_tokens": value,
        "prompt_tokens_details": {"cached_tokens": value}}})
    assert actual["total_tokens"] is None
    assert actual["cached_tokens"] is None
    assert actual["prompt_cache_hit_tokens"] is None


def test_explicit_cache_hit_takes_precedence_even_when_zero():
    actual = normalize_usage({"usage": {"prompt_cache_hit_tokens": 0,
        "prompt_cache_miss_tokens": 7, "prompt_tokens_details": {"cached_tokens": 9}}})
    assert actual["prompt_cache_hit_tokens"] == 0
    assert actual["cached_tokens"] == 9
    assert actual["prompt_cache_miss_tokens"] == 7


def test_missing_and_malformed_nested_usage_do_not_invent_totals_or_cache_misses():
    actual = normalize_usage({"usage": {"prompt_tokens": 8, "completion_tokens": 4,
        "prompt_tokens_details": [], "completion_tokens_details": False}})
    assert actual["total_tokens"] is None
    assert actual["cached_tokens"] is None
    assert actual["prompt_cache_miss_tokens"] is None
