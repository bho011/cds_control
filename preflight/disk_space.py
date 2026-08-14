"""Prüft freien Speicherplatz im Projektverzeichnis."""

from __future__ import annotations

import shutil

from .files_and_syntax import PROJECT_ROOT
from .report import PreflightReport

MIN_FREE_DISK_MB = 500
MAX_DISK_USAGE_PERCENT = 90


def check_disk_space(report: PreflightReport):
    usage = shutil.disk_usage(PROJECT_ROOT)

    total_mb = usage.total / 1024 / 1024
    free_mb = usage.free / 1024 / 1024
    used_percent = ((usage.total - usage.free) / usage.total) * 100

    detail = f"used={used_percent:.1f}% | free={free_mb:.0f} MB | total={total_mb:.0f} MB"

    if free_mb < MIN_FREE_DISK_MB:
        report.fail("Disk space", f"Free space below {MIN_FREE_DISK_MB} MB. {detail}")
    elif used_percent > MAX_DISK_USAGE_PERCENT:
        report.warn("Disk space", f"Disk usage above {MAX_DISK_USAGE_PERCENT}%. {detail}")
    else:
        report.ok("Disk space", detail)
