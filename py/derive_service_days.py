#!/usr/bin/env python3
"""
derive_service_days.py

Expand calendar.txt and calendar_dates.txt of a loaded GTFS snapshot schema
into one row per (service_id, date) on which the service actually runs.

Creates/replaces "{schema}".service_days with columns:
    service_id (text), date (date), isodow (smallint, 1 = Monday ... 7 = Sunday)

A service runs on a date if:
  1. the date lies within calendar.start_date..end_date and the weekday flag
     for that date is 1, OR calendar_dates has exception_type = 1 for it
  2. AND calendar_dates does not have exception_type = 2 for it

Services that exist only through calendar_dates (all-zero or missing weekly
calendar, additions only) are therefore included. This replaces the earlier
"all-zero weekly calendar = exception-only service, drop it" rule, which also
dropped regular service that the feed encodes purely as additions (e.g. all
of Metroselskabet's service, and new-timetable services starting mid-feed).

Scope: services used by at least one trip in view rail_trips (see
create_views.py). No area restriction here; that happens in
derive_trip_patterns.py. This script deliberately does not depend on
trip_patterns, so it can run before it.

The table covers the whole validity period of the feed. Choosing a week or
window to analyse is done downstream.

Usage:
    python py/derive_service_days.py --schema gtfs_20261019
"""

import argparse

from gtfs_common import add_connection_args, connect_from_args


def derive_service_days(conn, schema: str):
    with conn.cursor() as cur:
        cur.execute(f'DROP TABLE IF EXISTS "{schema}".service_days')
        cur.execute(f'''
            CREATE TABLE "{schema}".service_days AS
            WITH rail_services AS (
                SELECT DISTINCT service_id
                FROM "{schema}".rail_trips
            ),
            base AS (
                -- weekly pattern, expanded over the calendar's validity period
                SELECT c.service_id, g.d::date AS date
                FROM "{schema}".calendar c
                JOIN rail_services USING (service_id)
                CROSS JOIN LATERAL generate_series(
                    to_date(c.start_date, 'YYYYMMDD')::timestamp,
                    to_date(c.end_date,   'YYYYMMDD')::timestamp,
                    interval '1 day'
                ) AS g(d)
                WHERE (ARRAY[c.monday, c.tuesday, c.wednesday, c.thursday,
                             c.friday, c.saturday, c.sunday]
                      )[extract(isodow FROM g.d)::int] = '1'
            ),
            adds AS (
                SELECT cd.service_id, to_date(cd.date, 'YYYYMMDD') AS date
                FROM "{schema}".calendar_dates cd
                JOIN rail_services USING (service_id)
                WHERE cd.exception_type = '1'
            ),
            rems AS (
                SELECT cd.service_id, to_date(cd.date, 'YYYYMMDD') AS date
                FROM "{schema}".calendar_dates cd
                JOIN rail_services USING (service_id)
                WHERE cd.exception_type = '2'
            )
            SELECT s.service_id,
                   s.date,
                   extract(isodow FROM s.date)::smallint AS isodow
            FROM (
                (SELECT service_id, date FROM base
                 UNION
                 SELECT service_id, date FROM adds)
                EXCEPT
                SELECT service_id, date FROM rems
            ) s
        ''')
        cur.execute(f'ALTER TABLE "{schema}".service_days ADD PRIMARY KEY (service_id, date)')
        cur.execute(f'CREATE INDEX ON "{schema}".service_days (date)')
        cur.execute(f'ANALYZE "{schema}".service_days')
    conn.commit()

    with conn.cursor() as cur:
        cur.execute(f'''
            SELECT count(*), count(DISTINCT service_id), min(date), max(date)
            FROM "{schema}".service_days
        ''')
        n_rows, n_services, first_day, last_day = cur.fetchone()

        cur.execute(f'''
            SELECT count(DISTINCT service_id) FROM "{schema}".rail_trips
        ''')
        (n_rail_services,) = cur.fetchone()

        cur.execute(f'''
            SELECT count(*)
            FROM (SELECT DISTINCT service_id FROM "{schema}".rail_trips) rs
            WHERE NOT EXISTS (
                SELECT 1 FROM "{schema}".service_days sd
                WHERE sd.service_id = rs.service_id
            )
        ''')
        (n_without_days,) = cur.fetchone()

    print(f'Service days in "{schema}":')
    print(f"  rows:        {n_rows}")
    print(f"  date range:  {first_day} .. {last_day}")
    print(f"  services:    {n_services} with at least one service day "
          f"(of {n_rail_services} used by rail trips)")
    if n_without_days:
        print(f"  <-- {n_without_days} rail service(s) have no service days at all "
              f"(no weekly days, no additions, or fully cancelled), worth checking")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--schema", required=True, help="Snapshot schema, e.g. gtfs_20261019")
    add_connection_args(parser)
    args = parser.parse_args()

    conn = connect_from_args(args)
    derive_service_days(conn, args.schema)
    conn.close()


if __name__ == "__main__":
    main()
