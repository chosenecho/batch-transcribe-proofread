"""Calculate package bytes, SDK requests and cache headroom; accept account prices explicitly."""
import argparse
import json
import math


def estimate(package_bytes, *, part_size=8 * 1024**2, restores=0, months=1, rates=None):
    if (not package_bytes or any(type(n) is not int or n < 0 for n in package_bytes)
            or type(part_size) is not int or not 5 * 1024**2 <= part_size <= 64 * 1024**2
            or type(restores) is not int or restores < 0 or type(months) is not int or months < 1):
        raise ValueError("invalid package, part, restore or retention count")
    operations = dict(head_bucket=1, get_object=2 * len(package_bytes) + restores,
                      put_object=0, create_multipart_upload=0, upload_part=0, list_parts=0, complete_multipart_upload=0)
    for size in package_bytes:
        if size < part_size:
            operations["put_object"] += 1
        else:
            count = math.ceil(size / part_size)
            if count > 10000:
                raise ValueError("package exceeds configured multipart capacity")
            operations["create_multipart_upload"] += 1
            operations["upload_part"] += count
            operations["list_parts"] += 1  # Fresh upload has no existing parts.
            operations["complete_multipart_upload"] += 1
    stored = sum(package_bytes)
    # Restore count alone cannot identify package sizes. Bound each by largest.
    egress_upper = stored + restores * max(package_bytes)
    report = {"format": "object-storage-estimate/v1", "packages": len(package_bytes),
              "retained_bytes": stored, "retention_months": months, "new_upload_operations": operations,
              "upload_bytes": stored, "readback_and_restore_download_bytes_upper": egress_upper,
              "minimum_package_staging_bytes": max(package_bytes) + 16 * 1024**2,
              "multipart_buffer_bytes": part_size, "retry_and_resume_requests_included": False,
              "source_release_authorized": False, "account_limits_verified": False, "cost": None}
    if rates is not None:
        required = {"currency", "storage_per_gib_month", "egress_per_gib", "per_1000_requests"}
        if (set(rates) != required or not isinstance(rates["currency"], str) or not rates["currency"]
                or set(rates["per_1000_requests"]) != set(operations)):
            raise ValueError("rates must declare currency and every operation price explicitly")
        prices = [rates["storage_per_gib_month"], rates["egress_per_gib"], *rates["per_1000_requests"].values()]
        if any(type(value) not in {int, float} or not math.isfinite(value) or value < 0 for value in prices):
            raise ValueError("prices must be finite nonnegative numbers")
        storage = stored / 1024**3 * months * rates["storage_per_gib_month"]
        requests = sum(count / 1000 * rates["per_1000_requests"][operation] for operation, count in operations.items())
        bandwidth = egress_upper / 1024**3 * rates["egress_per_gib"]
        report["cost"] = {"currency": rates["currency"], "storage": storage, "requests": requests,
                          "bandwidth_upper": bandwidth, "total_upper": storage + requests + bandwidth,
                          "rates_source": "caller-supplied; no free-tier, quota or tax assumption"}
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--package-bytes", action="append", type=int, required=True)
    parser.add_argument("--part-size", type=int, default=8 * 1024**2)
    parser.add_argument("--restore-downloads", type=int, default=0)
    parser.add_argument("--months", type=int, default=1)
    parser.add_argument("--rates", help="Account-verified JSON price file; without it cost stays unknown")
    args = parser.parse_args()
    from pathlib import Path
    rates = json.loads(Path(args.rates).read_bytes()) if args.rates else None
    print(json.dumps(estimate(args.package_bytes, part_size=args.part_size, restores=args.restore_downloads,
                              months=args.months, rates=rates), indent=2))


if __name__ == "__main__":
    main()
