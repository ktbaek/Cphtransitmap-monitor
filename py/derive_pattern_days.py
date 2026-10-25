#!/usr/bin/env python3
"""
derive_pattern_days.py

Record when each trip pattern runs: one row per (pattern, date) on which at
least one trip of the pattern is in service.

Requires "{schema}".trip_patterns (derive_trip_patterns.py) and
"{schema}".service_days (derive_service_days.py).

Creates/replaces "{schema}".pattern_days with columns:
    pattern_id, date (date), isodow (smallint, 1 = Monday ... 7 = Sunday),
    n_trips (trips of the pattern running that day)

Covers every pattern and the whole feed, independent of the map: it is meant
for looking at frequency and regularity over time, e.g. plotting n_trips per
date for a set of patterns, or telling a one-week diversion from a permanent
routing. The map check (check_map_corridors.py) reads it to say when each
conflicting pattern runs, and its saved findings join to it on pattern_id.

pattern_id is only valid for the current trip_patterns: re-run this script
after re-deriving trip_patterns.

Usage:
    python py/derive_pattern_days.py --schema gtfs_20261019
"""

import argparse

from gtfs_common import add_connection_args, connect_from_args
from gtfs_weeks import require_table


def derive_pattern_days(conn, schema: str):
    require_table(conn, schema, "trip_patterns",
                  f"python py/derive_trip_patterns.py --schema {schema}")
    require_table(conn, schema, "service_days",
                  f"python py/derive_service_days.py --schema {schema}")

    with conn.cursor() as cur:
        cur.execute(f'DROP TABLE IF EXISTS "{schema}".pattern_days')
        cur.execute(f'''
            CREATE TABLE "{schema}".pattern_days AS
            SELECT
                tp.pattern_id,
                sd.date,
                sd.isodow,
                count(DISTINCT t.trip_id)::int AS n_trips
            FROM "{schema}".trip_patterns tp
            CROSS JOIN LATERAL unnest(tp.trip_ids) AS u(trip_id)
            JOIN "{schema}".rail_trips t ON t.trip_id = u.trip_id
            JOIN "{schema}".service_days sd ON sd.service_id = t.service_id
            GROUP BY tp.pattern_id, sd.date, sd.isodow
        ''')
        cur.execute(f'ALTER TABLE "{schema}".pattern_days ADD PRIMARY KEY (pattern_id, date)')
        cur.execute(f'CREATE INDEX ON "{schema}".pattern_days (date)')
        cur.execute(f'ANALYZE "{schema}".pattern_days')
    conn.commit()

    with conn.cursor() as cur:
        cur.execute(f'''
            SELECT count(*), count(DISTINCT pattern_id), min(date), max(date)
            FROM "{schema}".pattern_days
        ''')
        n_rows, n_with_days, first_day, last_day = cur.fetchone()
        cur.execute(f'SELECT count(*) FROM "{schema}".trip_patterns')
        (n_patterns,) = cur.fetchone()

    print(f'Pattern days in "{schema}":')
    print(f"  rows:        {n_rows}")
    print(f"  date range:  {first_day} .. {last_day}")
    print(f"  patterns:    {n_with_days} with at least one active day (of {n_patterns})")
    if n_with_days < n_patterns:
        print(f"  <-- {n_patterns - n_with_days} pattern(s) never run, worth checking")


def main():
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--schema", required=True, help="Snapshot schema, e.g. gtfs_20261019")
    add_connection_args(parser)
    args = parser.parse_args()

    conn = connect_from_args(args)
    derive_pattern_days(conn, args.schema)
    conn.close()


if __name__ == "__main__":
    main()
