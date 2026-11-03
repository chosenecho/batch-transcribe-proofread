import pytest

from scripts.estimate_object_storage import estimate


def test_storage_request_cache_and_explicit_price_calculation():
    report = estimate([1024, 11 * 1024**2], part_size=5 * 1024**2, restores=2)
    assert report["cost"] is None and not report["account_limits_verified"]
    operations = report["new_upload_operations"]
    assert operations["put_object"] == 1 and operations["upload_part"] == 3
    assert operations["get_object"] == 6 and operations["list_parts"] == 1
    assert report["readback_and_restore_download_bytes_upper"] == 33 * 1024**2 + 1024
    rates = {"currency": "test-units", "storage_per_gib_month": 2, "egress_per_gib": 3,
             "per_1000_requests": {operation: 4 for operation in operations}}
    priced = estimate([1024, 11 * 1024**2], part_size=5 * 1024**2, restores=2, rates=rates)
    cost = priced["cost"]
    assert cost["requests"] == pytest.approx(sum(operations.values()) / 1000 * 4)
    assert cost["total_upper"] == cost["storage"] + cost["requests"] + cost["bandwidth_upper"]


@pytest.mark.parametrize("sizes", [[], [-1], [True], [10**15]])
def test_invalid_storage_sizes_do_not_make_a_cost_promise(sizes):
    with pytest.raises(ValueError):
        estimate(sizes)


def test_cost_requires_complete_current_account_prices():
    with pytest.raises(ValueError, match="every operation"):
        estimate([1], rates={"currency": "x", "storage_per_gib_month": 0, "egress_per_gib": 0, "per_1000_requests": {}})
