#!/usr/bin/env python3
"""
derive_all.py

Run all derive steps for one loaded snapshot schema, in pipeline order:

    views            create_views.py            rail_* views
    service_days     derive_service_days.py     service_days
    trip_patterns    derive_trip_patterns.py    trip_patterns
    stop_triplets    derive_stop_triplets.py    stop_triplets
    pattern_days     derive_pattern_days.py     pattern_days
    service_pattern  derive_service_pattern.py  stop_service_pattern + view service_pattern

Stops at the first step that fails, and says which. Every step replaces its
own output, so re-running this on the same schema is safe. It does not load
the feed (load_gtfs.py refuses to overwrite a schema) and does not run the
map checks.

The service_pattern step is the only weekly one. By default it derives every
full Monday-to-Sunday week of the feed (--all-weeks); give --week (repeatable)
for specific weeks instead. --min-days is the number of days a service must
run in a week to count for it.

Re-run a single step with --only, or leave some out with --skip (both can be
repeated). Steps always run in pipeline order, and a step that needs an
earlier table that doesn't exist stops with a message naming the missing
step. The individual scripts still work on their own.

Usage:
    python py/derive_all.py --schema gtfs_20261019
    python py/derive_all.py --schema gtfs_20261019 --week 2026-10-19 --week 2026-12-14
    python py/derive_all.py --schema gtfs_20261019 --only service_pattern --all-weeks
    python py/derive_all.py --schema gtfs_20261019 --skip views
"""

import argparse
import time

from create_views import create_views
from derive_pattern_days import derive_pattern_days
from derive_service_days import derive_service_days
from derive_service_pattern import derive_service_patterns
from derive_stop_triplets import derive_stop_triplets
from derive_trip_patterns import derive_trip_patterns
from gtfs_common import add_connection_args, connect_from_args
from gtfs_weeks import choose_weeks

STEPS = ["views", "service_days", "trip_patterns", "stop_triplets",
         "pattern_days", "service_pattern"]


def run_step(name: str, conn, args):
    if name == "views":
        create_views(conn, args.schema)
    elif name == "service_days":
        derive_service_days(conn, args.schema)
    elif name == "trip_patterns":
        derive_trip_patterns(conn, args.schema)
    elif name == "stop_triplets":
        derive_stop_triplets(conn, args.schema)
    elif name == "pattern_days":
        derive_pattern_days(conn, args.schema)
    elif name == "service_pattern":
        # weeks are resolved here, after service_days exists
        weeks = choose_weeks(conn, args.schema, args.week, args.all_weeks or not args.week)
        derive_service_patterns(conn, args.schema, weeks, args.min_days)
    else:
        raise ValueError(f"unknown step {name!r}")


def main():
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--schema", required=True, help="Snapshot schema, e.g. gtfs_20261019")
    parser.add_argument("--only", action="append", choices=STEPS, metavar="STEP",
                        help=f"Run only this step (repeatable). Steps: {', '.join(STEPS)}")
    parser.add_argument("--skip", action="append", choices=STEPS, default=[], metavar="STEP",
                        help="Leave this step out (repeatable)")
    parser.add_argument("--week", action="append",
                        help="Monday of a week for the service_pattern step, YYYY-MM-DD "
                             "(repeatable; default: every full week of the feed)")
    parser.add_argument("--all-weeks", action="store_true",
                        help="Derive every full week of the feed (the default without --week)")
    parser.add_argument("--min-days", type=int, default=1,
                        help="A service counts for a week if it runs on at least this "
                             "many days of it (default: 1)")
    add_connection_args(parser)
    args = parser.parse_args()

    if args.all_weeks and args.week:
        parser.error("use either --all-weeks or --week, not both")

    selected = [s for s in STEPS
                if (not args.only or s in args.only) and s not in args.skip]
    if not selected:
        parser.error("no steps left to run")

    conn = connect_from_args(args)
    timings = []
    total_start = time.perf_counter()

    for number, name in enumerate(selected, start=1):
        print(f"\n##### [{number}/{len(selected)}] {name} #####")
        start = time.perf_counter()
        try:
            run_step(name, conn, args)
        except BaseException:
            conn.rollback()
            elapsed = time.perf_counter() - start
            print(f"\nStep '{name}' failed after {elapsed:.1f}s; "
                  f"steps after it were not run.")
            raise
        timings.append((name, time.perf_counter() - start))

    print("\n##### Done #####")
    for name, seconds in timings:
        print(f"  {name:<16} {seconds:7.1f}s")
    print(f"  {'total':<16} {time.perf_counter() - total_start:7.1f}s")
    conn.close()


if __name__ == "__main__":
    main()
