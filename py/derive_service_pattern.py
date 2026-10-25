#!/usr/bin/env python3
"""
derive_service_pattern.py

Classify, per (route_id, stop_id), which day-type / time-of-day service
categories are actually served in one chosen week of this snapshot:
    day_type:  'weekday' | 'saturday' | 'sunday_holiday'
    time_band: 'peak' | 'daytime' | 'evening'   (weekdays only)
               'all'                             (saturday / sunday_holiday
                                                    — not split by time of day)

Requires "{schema}".service_days (see derive_service_days.py).

The week is a Monday-to-Sunday week, given as the date of its Monday
(--week, repeatable; --all-weeks for every full week of the feed; default:
the first full week of the feed). Each week is derived in its own
transaction, and re-running a week replaces its rows. Day types come from the
actual dates a service runs in that week (service_days): a trip is a weekday
trip if its service runs on at least one Monday-Friday of the week, a
Saturday trip if it runs on the Saturday, and a sunday_holiday trip if it
runs on the Sunday. A service must run on at least --min-days days of the
week to count at all (default 1, so a one-day diversion counts).
Services encoded purely through calendar_dates.txt are therefore included.
Public holidays inside the chosen week are not treated specially: a holiday
Monday just looks like a weekday that happened to have a Sunday timetable.
Prefer a week without holidays.

Stores only rows that ARE served — a presence table. A stop's absence for
a given (route_id, day_type, time_band) means "not served that way".

Creates "{schema}".stop_service_pattern if it does not exist, and (re)fills
the rows for the chosen week; other weeks are left untouched. Columns:
    week_start, route_id, stop_id, day_type, time_band

Also creates/replaces view "{schema}".service_pattern, which collapses the
presence rows above into one named service_category per
(week_start, route_id, stop_id) — e.g. all_times, peak_only,
weekend_and_evening_only — for check_map_service_patterns.py and manual
inspection. Filter the view on week_start.

Only stops with stops.in_cph_area = true are included — a stop entirely
outside the area of interest (on a fully-external DSB regional/IC line,
mainly) is left out, same restriction as trip_patterns.

Usage:
    python py/derive_service_pattern.py --schema gtfs_20261019
    python py/derive_service_pattern.py --schema gtfs_20261019 --week 2026-12-14
    python py/derive_service_pattern.py --schema gtfs_20261019 --week 2026-10-19 --week 2026-12-14
    python py/derive_service_pattern.py --schema gtfs_20261019 --all-weeks --min-days 2
"""

import argparse
from datetime import date, timedelta

from gtfs_common import add_connection_args, connect_from_args
from gtfs_weeks import choose_weeks, drop_if_missing_week_column

# Weekday hour bands, as [start, end) in the local 24h clock. GTFS times
# past midnight are written as e.g. "25:10:00" for a trip that departs at
# 01:10 the next service day — since that's hour >= 24, it already falls
# outside every band below and lands in "evening" without needing any
# modulo arithmetic. Saturday/Sunday are not split by time of day at all.
PEAK_HOURS = [(6, 9), (14, 17)]
DAYTIME_HOURS = (11, 13)
EVENING_HOURS = (21, 24)


def build_time_band_case_sql() -> str:
    peak_conditions = " OR ".join(
        f"(hour >= {start} AND hour < {end})" for start, end in PEAK_HOURS
    )
    return f'''
        CASE
            WHEN {peak_conditions} THEN 'peak'
            WHEN hour >= {DAYTIME_HOURS[0]} AND hour < {DAYTIME_HOURS[1]} THEN 'daytime'
            WHEN hour >= {EVENING_HOURS[0]} AND hour < {EVENING_HOURS[1]} THEN 'evening'
            ELSE NULL
        END
    '''


def ensure_table(conn, schema: str):
    """Create stop_service_pattern if missing; recreate it if it predates week_start."""
    with conn.cursor() as cur:
        # CASCADE also drops the old service_pattern view, which is recreated below
        drop_if_missing_week_column(cur, schema, "stop_service_pattern", cascade=True)
        cur.execute(f'''
            CREATE TABLE IF NOT EXISTS "{schema}".stop_service_pattern (
                week_start date NOT NULL,
                route_id text NOT NULL,
                stop_id text NOT NULL,
                day_type text NOT NULL,
                time_band text NOT NULL,
                PRIMARY KEY (week_start, route_id, stop_id, day_type, time_band)
            )
        ''')


def derive_service_pattern(conn, schema: str, week_start: date, min_days: int = 1):
    """Fill stop_service_pattern for one week. Does not commit; the caller does."""
    ensure_table(conn, schema)
    time_band_case = build_time_band_case_sql()

    params = {"week": week_start, "min_days": min_days}
    with conn.cursor() as cur:
        cur.execute(f'DELETE FROM "{schema}".stop_service_pattern WHERE week_start = %(week)s',
                    params)
        cur.execute(f'''
            INSERT INTO "{schema}".stop_service_pattern
                (week_start, route_id, stop_id, day_type, time_band)
            WITH week_service_days AS (
                SELECT
                    service_id,
                    bool_or(isodow BETWEEN 1 AND 5) AS is_weekday,
                    bool_or(isodow = 6) AS is_saturday,
                    bool_or(isodow = 7) AS is_sunday
                FROM "{schema}".service_days
                WHERE date >= %(week)s::date AND date < %(week)s::date + 7
                GROUP BY service_id
                HAVING count(*) >= %(min_days)s
            ),
            trip_day_types AS (
                SELECT
                    t.trip_id,
                    t.route_id,
                    w.is_weekday,
                    w.is_saturday,
                    w.is_sunday
                FROM "{schema}".rail_trips t
                JOIN week_service_days w ON w.service_id = t.service_id
            ),
            stop_time_bands AS (
                SELECT
                    trip_id,
                    stop_id,
                    {time_band_case} AS time_band
                FROM (
                    SELECT
                        st.trip_id,
                        st.stop_id,
                        split_part(st.arrival_time, ':', 1)::int AS hour
                    FROM "{schema}".stop_times st
                    JOIN "{schema}".stops s USING (stop_id)
                    WHERE st.arrival_time IS NOT NULL AND st.arrival_time <> ''
                      AND s.in_cph_area
                       AND NOT (
                          COALESCE(NULLIF(st.pickup_type, ''), '0') = '1'
                          AND COALESCE(NULLIF(st.drop_off_type, ''), '0') = '1'
                      )
                ) hourly
            ),
            combined AS (
                SELECT d.route_id, b.stop_id, 'weekday' AS day_type, b.time_band
                FROM trip_day_types d
                JOIN stop_time_bands b ON b.trip_id = d.trip_id
                WHERE d.is_weekday AND b.time_band IS NOT NULL

                UNION

                SELECT d.route_id, b.stop_id, 'saturday' AS day_type, 'all' AS time_band
                FROM trip_day_types d
                JOIN stop_time_bands b ON b.trip_id = d.trip_id
                WHERE d.is_saturday

                UNION

                SELECT d.route_id, b.stop_id, 'sunday_holiday' AS day_type, 'all' AS time_band
                FROM trip_day_types d
                JOIN stop_time_bands b ON b.trip_id = d.trip_id
                WHERE d.is_sunday
            )
            SELECT DISTINCT %(week)s::date, route_id, stop_id, day_type, time_band
            FROM combined
        ''', params)

        cur.execute(f'''
            SELECT day_type, time_band, count(*)
            FROM "{schema}".stop_service_pattern
            WHERE week_start = %(week)s
            GROUP BY day_type, time_band
            ORDER BY day_type, time_band
        ''', params)
        rows = cur.fetchall()

    week_end = week_start + timedelta(days=6)
    print(f'stop_service_pattern rows in "{schema}", week {week_start} .. {week_end} '
          f'(services running >= {min_days} day(s) that week):')
    if not rows:
        print("  (no rows: check that service_days covers this week)")
    for day_type, time_band, n in rows:
        print(f"  {day_type:<15} {time_band:<8} {n} (route, stop) combinations served")


def create_service_pattern_view(conn, schema: str):
    """(Re)create the service_pattern view. Does not commit; the caller does."""
    with conn.cursor() as cur:
        cur.execute(f'DROP VIEW IF EXISTS "{schema}".service_pattern')
        cur.execute(f'''
            CREATE VIEW "{schema}".service_pattern AS
            WITH flags AS (
                SELECT
                    sp.week_start,
                    sp.route_id,
                    r.route_short_name,
                    sp.stop_id,
                    bool_or(day_type = 'weekday' AND time_band = 'peak')    AS has_peak,
                    bool_or(day_type = 'weekday' AND time_band = 'daytime') AS has_daytime,
                    bool_or(day_type = 'weekday' AND time_band = 'evening') AS has_evening,
                    bool_or(day_type = 'saturday')                          AS has_saturday,
                    bool_or(day_type = 'sunday_holiday')                    AS has_sunday
                FROM "{schema}".stop_service_pattern sp
                JOIN "{schema}".rail_routes r USING (route_id)
                GROUP BY sp.week_start, route_id, route_short_name, stop_id
            ),
            labeled AS (
                SELECT
                    *,
                    array_remove(ARRAY[
                        CASE WHEN has_peak     THEN 'weekday_peak' END,
                        CASE WHEN has_daytime  THEN 'weekday_daytime' END,
                        CASE WHEN has_evening  THEN 'weekday_evening' END,
                        CASE WHEN has_saturday THEN 'saturday' END,
                        CASE WHEN has_sunday   THEN 'sunday_holiday' END
                    ], NULL) AS categories_present
                FROM flags
            )
            SELECT
                week_start,
                route_id,
                route_short_name,
                stop_id,
                s.stop_name,
                CASE
                    WHEN array_length(categories_present, 1) = 5 THEN 'all_times'
                    WHEN categories_present = ARRAY['weekday_peak']            THEN 'peak_only'
                    WHEN categories_present = ARRAY['saturday', 'sunday_holiday'] THEN 'weekend_only'
                    WHEN categories_present = ARRAY['weekday_evening', 'saturday', 'sunday_holiday'] THEN 'weekend_and_evening_only'
                    WHEN categories_present = ARRAY['weekday_peak', 'weekday_daytime', 'saturday', 'sunday_holiday'] THEN 'weekend_and_daytime_only'
                    WHEN categories_present = ARRAY['weekday_peak', 'weekday_daytime'] THEN 'weekday_daytime_only'
                    WHEN categories_present = ARRAY['weekday_peak', 'weekday_daytime', 'weekday_evening'] THEN 'weekday_only'
                    ELSE array_to_string(categories_present, '+')
                END AS service_category
            FROM labeled
            JOIN "{schema}".stops s USING (stop_id)
            ORDER BY week_start, route_id, stop_id
        ''')
    print(f'View "{schema}".service_pattern created (filter it on week_start).')


def main():
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--schema", required=True, help="Snapshot schema, e.g. gtfs_20261019")
    parser.add_argument("--week", action="append",
                        help="Monday of a week to derive, YYYY-MM-DD (repeatable; "
                             "default: first full week of the feed)")
    parser.add_argument("--all-weeks", action="store_true",
                        help="Derive every full week inside the feed's service days")
    parser.add_argument("--min-days", type=int, default=1,
                        help="A service counts for a week if it runs on at least this "
                             "many days of it (default: 1)")
    add_connection_args(parser)
    args = parser.parse_args()

    if args.all_weeks and args.week:
        parser.error("use either --all-weeks or --week, not both")

    conn = connect_from_args(args)
    week_starts = choose_weeks(conn, args.schema, args.week, args.all_weeks)

    for week_start in week_starts:
        print(f"\n=== Week {week_start} ===")
        try:
            derive_service_pattern(conn, args.schema, week_start, args.min_days)
            conn.commit()
        except Exception:
            conn.rollback()
            print(f"Week {week_start} failed; its rows were left unchanged.")
            raise

    create_service_pattern_view(conn, args.schema)
    conn.commit()
    conn.close()


if __name__ == "__main__":
    main()
