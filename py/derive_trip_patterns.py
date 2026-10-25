#!/usr/bin/env python3
"""
derive_trip_patterns.py

Compute one row per distinct stop-sequence ("pattern") for every
route/direction in a loaded GTFS snapshot schema. A trip pattern is
composed of all trips that are identical with regard to their stop
sequence.

Requires "{schema}".service_days (see derive_service_days.py).

Covers the whole feed, not a single week: a pattern is a property of the
trips, and when it runs is recorded separately in pattern_days
(derive_pattern_days.py). A trip is included if its service runs on at least
one day in service_days. This replaces the earlier rule that excluded every
trip whose service has an all-zero weekly calendar: services encoded purely
through calendar_dates.txt (which includes regular service for some
agencies, and new timetables starting mid-feed) are now included, and
services that never run are left out. Short-lived variants (diversions,
one-off services) are included too; pattern_days tells them apart from
permanent patterns.

Creates/replaces "{schema}".trip_patterns with columns:
    pattern_id, route_id, route_short_name, direction_id,
    first_stop, last_stop, stop_ids, stop_names, stop_in_cph_area
    (arrays, in visiting order), n_trips (trips running this exact pattern
    in the whole feed), trip_ids (which trips run this exact pattern)

pattern_id is a row identifier, unique within this table (not stable across
snapshots, and not stable if the table is re-derived) — used by
derive_stop_triplets.py, derive_pattern_days.py and the saved map check
findings. Re-deriving this table therefore means re-running
derive_stop_triplets.py and derive_pattern_days.py, and the map check.

first_stop/last_stop are just stop_names[1]/stop_names[-1], kept as their
own columns so a pattern's rough extent is visible without expanding
stop_names.

trip_headsign is not part of the grouping.

Only patterns that touch the area at all are kept — a route entirely
outside Copenhagen (fully-external DSB regional/IC lines, mainly) is
dropped rather than cluttering the table. A route that only partly
overlaps (e.g. a regional line passing through) is kept in full, with
stop_in_cph_area showing which of its stops are actually in scope.

Stops where the train passes through without actually stopping are
excluded from the sequence: per GTFS, pickup_type = 1 AND drop_off_type = 1
means no boarding and no alighting there, so it isn't a real stop on this
pattern as far as a passenger-facing view is concerned — matches the same
filter in derive_service_pattern.py.

A route/direction with more than one row here has more than one distinct
stop sequence in service (branches, skip-stop, short-turns, etc.).

Usage:
    python py/derive_trip_patterns.py --schema gtfs_20261019
"""

import argparse

from gtfs_common import add_connection_args, connect_from_args
from gtfs_weeks import require_table


def derive_trip_patterns(conn, schema: str):
    require_table(conn, schema, "service_days",
                  f"python py/derive_service_days.py --schema {schema}")

    with conn.cursor() as cur:
        cur.execute(f'DROP TABLE IF EXISTS "{schema}".trip_patterns')
        cur.execute(f'''
            CREATE TABLE "{schema}".trip_patterns AS
            WITH active_services AS (
                SELECT DISTINCT service_id
                FROM "{schema}".service_days
            ),
            trip_seqs AS (
                SELECT
                    t.trip_id,
                    t.route_id,
                    r.route_short_name,
                    t.direction_id,
                    array_agg(st.stop_id ORDER BY st.stop_sequence::int) AS stop_ids,
                    array_agg(s.stop_name ORDER BY st.stop_sequence::int) AS stop_names,
                    array_agg(s.in_cph_area ORDER BY st.stop_sequence::int) AS stop_in_cph_area,
                    bool_or(s.in_cph_area) AS touches_cph_area
                FROM "{schema}".rail_trips t
                JOIN active_services USING (service_id)
                JOIN "{schema}".stop_times st USING (trip_id)
                JOIN "{schema}".stops s USING (stop_id)
                JOIN "{schema}".routes r USING (route_id)
                WHERE NOT (
                    COALESCE(NULLIF(st.pickup_type, ''), '0') = '1'
                    AND COALESCE(NULLIF(st.drop_off_type, ''), '0') = '1'
                )
                GROUP BY t.trip_id, t.route_id, r.route_short_name, t.direction_id
            )
            SELECT
                row_number() OVER (ORDER BY route_id, direction_id, stop_ids) AS pattern_id,
                route_id,
                route_short_name,
                direction_id,
                stop_names[1] AS first_stop,
                stop_names[array_length(stop_names, 1)] AS last_stop,
                stop_ids,
                stop_names,
                stop_in_cph_area,
                count(*) AS n_trips,
                array_agg(trip_id ORDER BY trip_id) AS trip_ids
            FROM trip_seqs
            WHERE touches_cph_area
            GROUP BY route_id, route_short_name, direction_id,
                     stop_ids, stop_names, stop_in_cph_area
        ''')
        cur.execute(f'ALTER TABLE "{schema}".trip_patterns ADD PRIMARY KEY (pattern_id)')
    conn.commit()

    with conn.cursor() as cur:
        cur.execute(f'''
            SELECT route_short_name, direction_id, count(*) AS n_patterns, sum(n_trips) AS n_trips
            FROM "{schema}".trip_patterns
            GROUP BY route_short_name, direction_id
            ORDER BY route_short_name, direction_id
        ''')
        print(f'Patterns per route/direction in "{schema}" (whole feed, area-relevant only):')
        for route_short_name, direction_id, n_patterns, n_trips in cur.fetchall():
            flag = "" if n_patterns == 1 else "  <-- multiple patterns, worth checking"
            print(f"  {str(route_short_name):<8} dir={direction_id}  "
                  f"{n_patterns} pattern(s), {n_trips} trips{flag}")


def main():
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--schema", required=True, help="Snapshot schema, e.g. gtfs_20261019")
    add_connection_args(parser)
    args = parser.parse_args()

    conn = connect_from_args(args)
    derive_trip_patterns(conn, args.schema)
    conn.close()


if __name__ == "__main__":
    main()
