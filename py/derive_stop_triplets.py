#!/usr/bin/env python3
"""
derive_stop_triplets.py

Builds (prev_stop, stop, next_stop) triplets from trip_patterns.

Requires "{schema}".trip_patterns to already exist (run
derive_trip_patterns.py first) — triplets are built from it, so they
inherit every filter already baked into that table: area restriction,
pass-through-stop exclusion, and the requirement that a trip's service
runs on at least one day (service_days). Covers the whole feed, not a
single week; when each pattern runs is in pattern_days
(derive_pattern_days.py).

Creates one table:

  stop_triplets — one row per (pattern, position), keeping
      route_id/direction_id/pattern_id. The first stop of a pattern has
      prev_stop_id = NULL, the last has next_stop_id = NULL. These NULLs
      are meaningful (route termini), not missing data, and are kept
      rather than filtered out, since a terminus changing between
      periods is itself a real network change.

pattern_id refers to the trip_patterns row as it is now: if trip_patterns is
re-derived, re-run this script too.

Usage:
    python py/derive_stop_triplets.py --schema gtfs_20261019
"""

import argparse

from gtfs_common import add_connection_args, connect_from_args
from gtfs_weeks import require_table


def derive_stop_triplets(conn, schema: str):
    require_table(conn, schema, "trip_patterns",
                  f"python py/derive_trip_patterns.py --schema {schema}")

    with conn.cursor() as cur:
        cur.execute(f'DROP TABLE IF EXISTS "{schema}".stop_triplets')
        cur.execute(f'''
            CREATE TABLE "{schema}".stop_triplets AS
            WITH pattern_stops AS (
                SELECT
                    tp.pattern_id,
                    tp.route_id,
                    tp.route_short_name,
                    tp.direction_id,
                    tp.n_trips,
                    u.stop_id,
                    u.ord
                FROM "{schema}".trip_patterns tp,
                     unnest(tp.stop_ids) WITH ORDINALITY AS u(stop_id, ord)
            )
            SELECT
                curr.route_id,
                curr.route_short_name,
                curr.direction_id,
                curr.pattern_id,
                curr.n_trips,
                prev.stop_id AS prev_stop_id,
                curr.stop_id,
                next.stop_id AS next_stop_id
            FROM pattern_stops curr
            LEFT JOIN pattern_stops prev
              ON prev.pattern_id = curr.pattern_id AND prev.ord = curr.ord - 1
            LEFT JOIN pattern_stops next
              ON next.pattern_id = curr.pattern_id AND next.ord = curr.ord + 1
        ''')
        cur.execute(f'CREATE INDEX ON "{schema}".stop_triplets (pattern_id)')

    conn.commit()

    with conn.cursor() as cur:
        cur.execute(f'SELECT count(*) FROM "{schema}".stop_triplets')
        n_triplets = cur.fetchone()[0]

    print(f'"{schema}": {n_triplets} stop_triplets rows.')


def main():
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--schema", required=True, help="Snapshot schema, e.g. gtfs_20261019")
    add_connection_args(parser)
    args = parser.parse_args()

    conn = connect_from_args(args)
    derive_stop_triplets(conn, args.schema)
    conn.close()


if __name__ == "__main__":
    main()
