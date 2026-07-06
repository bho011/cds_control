#!/usr/bin/env python3
"""
Compatibility wrapper for the old refill/sensor/drain test entry point.

The real implementation lives in:
    process.water_cycle

This wrapper exists only so old commands do not silently use outdated
duplicated process code.
"""

from process.water_cycle import main


if __name__ == "__main__":
    main()
