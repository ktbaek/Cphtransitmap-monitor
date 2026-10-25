"""
gtfs_weeks.py

Shared helpers for the scripts that work on one Monday-to-Sunday week
(derive_service_pattern.py, check_map_service_patterns.py), plus a small guard
used by the other derive/check scripts.

A "week" is identified by the date of its Monday (week_start). Only
stop_service_pattern / service_pattern are derived per week, because the map
shows service as a weekly pattern. The topology tables (trip_patterns,
stop_triplets, pattern_days) cover the whole feed.
"""

from __future__ import annotations

from datetime import date, timedelta


def _table_exists(cur, schema: str, table: str) -> bool:
    cur.execute("SELECT to_regclass(%s)", (f'"{schema}"."{table}"',))
    return cur.fetchone()[0] is not None


def _has_week_column(cur, schema: str, table: str) -> bool:
    cur.execute("""
        SELECT 1 FROM information_schema.columns
        WHERE table_schema = %s AND table_name = %s AND column_name = 'week_start'
    """, (schema, table))
    return cur.fetchone() is not None


def require_table(conn, schema: str, table: str, hint: str):
    """Exit with a pointer to the script that creates `table` if it doesn't exist."""
    with conn.cursor() as cur:
        if not _table_exists(cur, schema, table):
            raise SystemExit(f'"{schema}".{table} does not exist. Run: {hint}')


def service_day_range(conn, schema: str):
    """(first_day, last_day) of "{schema}".service_days; exits if unavailable."""
    require_table(conn, schema, "service_days",
                  f"python py/derive_service_days.py --schema {schema}")
    with conn.cursor() as cur:
        cur.execute(f'SELECT min(date), max(date) FROM "{schema}".service_days')
        first_day, last_day = cur.fetchone()
    if first_day is None:
        raise SystemExit(f'"{schema}".service_days is empty.')
    return first_day, last_day


def resolve_week(conn, schema: str, week) -> date:
    """
    Return the Monday of the week to work on, validated against service_days.
    week: 'YYYY-MM-DD' string (must be a Monday) or None for the first full
    week of the feed.
    """
    first_day, last_day = service_day_range(conn, schema)

    if week is None:
        # first Monday on or after the first service day
        week_start = first_day + timedelta(days=(7 - first_day.weekday()) % 7)
    else:
        week_start = date.fromisoformat(week)
        if week_start.weekday() != 0:
            raise SystemExit(f"--week must be a Monday; {week_start} is a "
                             f"{week_start.strftime('%A')}.")

    week_end = week_start + timedelta(days=6)
    if week_start < first_day or week_end > last_day:
        raise SystemExit(
            f"Week {week_start} .. {week_end} is not fully inside the service "
            f"days of this snapshot ({first_day} .. {last_day})."
        )
    return week_start


def full_weeks(conn, schema: str) -> list:
    """Mondays of every Monday-to-Sunday week fully inside service_days."""
    first_day, last_day = service_day_range(conn, schema)
    monday = first_day + timedelta(days=(7 - first_day.weekday()) % 7)
    weeks = []
    while monday + timedelta(days=6) <= last_day:
        weeks.append(monday)
        monday += timedelta(days=7)
    return weeks


def choose_weeks(conn, schema: str, weeks, all_weeks: bool) -> list:
    """
    Weeks to derive from the command line: every full week (all_weeks), the
    listed weeks, or by default the first full week of the feed.
    """
    if all_weeks:
        chosen = full_weeks(conn, schema)
        if not chosen:
            raise SystemExit("No full Monday-to-Sunday week inside the service days.")
        return chosen
    if weeks:
        return sorted({resolve_week(conn, schema, w) for w in weeks})
    return [resolve_week(conn, schema, None)]


def existing_weeks(conn, schema: str, table: str) -> list:
    """Distinct week_start values present in a derived table (empty if none)."""
    with conn.cursor() as cur:
        if not _table_exists(cur, schema, table) or not _has_week_column(cur, schema, table):
            return []
        cur.execute(f'SELECT DISTINCT week_start FROM "{schema}"."{table}" ORDER BY 1')
        return [row[0] for row in cur.fetchall()]


def pick_week(conn, schema: str, table: str, week, hint: str) -> date:
    """
    Choose the week to read from a derived table, for scripts that build on it.
    With an explicit week, it must exist in the table. Without one, the table
    must hold exactly one week. Anything else exits with a message listing the
    available weeks and `hint` (the command that derives the missing week).
    """
    available = existing_weeks(conn, schema, table)

    if week is not None:
        week_start = date.fromisoformat(week)
        if week_start not in available:
            listing = ", ".join(str(w) for w in available) or "none"
            raise SystemExit(
                f'"{schema}".{table} has no rows for week {week_start}. '
                f"Weeks available: {listing}. Derive it with: {hint}"
            )
        return week_start

    if len(available) == 1:
        return available[0]
    if not available:
        raise SystemExit(f'"{schema}".{table} has no rows. Derive it with: {hint}')
    listing = ", ".join(str(w) for w in available)
    raise SystemExit(
        f'"{schema}".{table} holds several weeks ({listing}); choose one with --week.'
    )


def drop_if_missing_week_column(cur, schema: str, table: str, cascade: bool = False):
    """Drop a derived table left over from before week_start existed."""
    if _table_exists(cur, schema, table) and not _has_week_column(cur, schema, table):
        print(f'Dropping old "{schema}".{table} (no week_start column).')
        cur.execute(f'DROP TABLE "{schema}"."{table}"' + (" CASCADE" if cascade else ""))
