#!/usr/bin/env python3
"""
check_map_corridors.py

Check GTFS (prev, stop, next) triplets against the map's lines, encoded by hand
in config/map_corridors.yml as canonical corridors (maximum length, all stops,
in order). Groups map agencies to modes. A corridor may list the routes
(route_short_name) drawn on it: triplets of a listed route are checked only
against that route's corridors, so an extension along another line's corridor
is caught. Triplets of unlisted routes are checked against every corridor of
their group.

A triplet is consistent if one corridor in scope contains all its non-NULL
stops in strictly increasing or strictly decreasing position. For corridor
ABCDEF, CDE, ACE (skip-stop) and AF-NULL (terminus) are all consistent. NULL
ends are wildcards, so short-turns are never conflicts. Ring corridors
(ring: true) may wrap around. Stops outside the Copenhagen area count as NULL,
and triplets centred on one are skipped.

Reports, per group: corridor stops not in the feed (closed/renamed station or
typo), unknown stops (served, but on no corridor in scope), conflicts (all
stops known, but no corridor in scope orders them) and unserved corridor stops
(on the map, but not served by the corridor's routes, or by the group if it
lists none). Groups without corridors are skipped.

The whole feed is checked at once. Every conflict line says when the
conflicting patterns run (first and last day, number of active days, median
trips per day, from pattern_days), so a one-week diversion looks different
from a permanent routing, and lists their pattern_ids. --from/--to restrict
the check to patterns that run at least one day in that window, and the
activity shown to that window.

The unknown-stop and conflict findings are also saved to
"{schema}".map_check_findings (replaced on every run; skip with --no-save):
one row per finding and pattern_id, so they can be joined to pattern_days and
plotted without copying ids:
    finding_id, kind ('conflict' | 'unknown_stop'), group_name, scope,
    routes, prev_stop_name, stop_name, next_stop_name, pattern_id,
    checked_at, corridors_file, window_from, window_to
finding_id matches the #number printed on each line. pattern_ids are only
valid for the current trip_patterns, so re-run the check after re-deriving.

Requires stop_triplets (derive_stop_triplets.py) and pattern_days
(derive_pattern_days.py).

Usage:
    python py/check_map_corridors.py --schema gtfs_20261019
    python py/check_map_corridors.py --schema gtfs_20261019 --from 2026-10-19 --to 2026-12-12
"""

import argparse
from collections import defaultdict
from datetime import date
from pathlib import Path
from statistics import median

import yaml
from psycopg2.extras import execute_values

from gtfs_common import add_connection_args, connect_from_args
from gtfs_weeks import require_table


def is_ordered(positions, ring_size=None):
    """True if positions strictly advance in one direction (modulo ring_size for rings)."""
    steps = list(zip(positions, positions[1:]))
    if not steps:
        return True
    if ring_size is None:
        return all(a < b for a, b in steps) or all(a > b for a, b in steps)
    for diffs in ([(b - a) % ring_size for a, b in steps],
                  [(a - b) % ring_size for a, b in steps]):
        if 0 not in diffs and sum(diffs) < ring_size:
            return True
    return False


def is_consistent(stops, corridors):
    """stops: non-NULL stop_ids in triplet order. corridors: list of corridor dicts."""
    for c in corridors:
        index = c["index"]
        if all(s in index for s in stops) and is_ordered([index[s] for s in stops], c["ring_size"]):
            return True
    return False


def normalize_name(name: str) -> str:
    """Collapse whitespace runs, since some feed stop_names contain stray double spaces."""
    return " ".join(name.split())


def load_corridors(path: Path, name_to_ids):
    with open(path) as f:
        cfg = yaml.safe_load(f)

    agency_group = {}
    for group, spec in cfg["groups"].items():
        for agency_id in spec["agencies"]:
            agency_group[str(agency_id)] = group

    corridors = defaultdict(list)        # group -> [corridor]
    route_corridors = defaultdict(list)  # (group, route_short_name) -> [corridor]
    problems = []
    for name, spec in (cfg.get("corridors") or {}).items():
        if spec.get("group") not in cfg["groups"]:
            problems.append(f"{name}: unknown or missing group {spec.get('group')!r}")
            continue
        if "stops" not in spec:
            problems.append(f"{name}: no 'stops' list")
            continue
        stop_names = [normalize_name(s) for s in spec["stops"]]
        index = {}
        not_in_feed = []
        for pos, stop_name in enumerate(stop_names):
            if stop_name not in name_to_ids:
                not_in_feed.append(stop_name)
                continue
            for stop_id in name_to_ids[stop_name]:
                index[stop_id] = pos
        corridor = {
            "name": name,
            "group": spec["group"],
            "index": index,
            "ring_size": len(stop_names) if spec.get("ring") else None,
            "stops": stop_names,
            "not_in_feed": not_in_feed,
            "routes": [str(r) for r in spec.get("routes") or []],
        }
        corridors[spec["group"]].append(corridor)
        for route in corridor["routes"]:
            route_corridors[(spec["group"], route)].append(corridor)

    if problems:
        raise SystemExit("Problems in corridor file:\n  " + "\n  ".join(problems))
    return agency_group, corridors, route_corridors


def fetch_stops(conn, schema: str):
    with conn.cursor() as cur:
        cur.execute(f'SELECT stop_id, stop_name, in_cph_area FROM "{schema}".rail_stops')
        rows = cur.fetchall()
    stop_info = {stop_id: (name, bool(in_area)) for stop_id, name, in_area in rows}
    name_to_ids = defaultdict(list)
    for stop_id, name, _ in rows:
        name_to_ids[normalize_name(name)].append(stop_id)
    return stop_info, name_to_ids


def date_conditions(date_from, date_to):
    """SQL conditions and parameters restricting pattern_days.date to the window."""
    conditions, params = [], []
    if date_from:
        conditions.append("date >= %s")
        params.append(date_from)
    if date_to:
        conditions.append("date <= %s")
        params.append(date_to)
    return conditions, params


def fetch_triplets(conn, schema: str, date_from, date_to):
    """Returns {(agency_id, route_short_name, prev, stop, next): [pattern_ids]}.
    With a window, only patterns running at least one day in it are included."""
    conditions, params = date_conditions(date_from, date_to)
    where = ""
    if conditions:
        where = (f'WHERE t.pattern_id IN (SELECT pattern_id FROM "{schema}".pattern_days '
                 f'WHERE {" AND ".join(conditions)})')
    with conn.cursor() as cur:
        cur.execute(f'''
            SELECT r.agency_id, t.route_short_name, t.prev_stop_id, t.stop_id, t.next_stop_id,
                   array_agg(DISTINCT t.pattern_id ORDER BY t.pattern_id)
            FROM "{schema}".stop_triplets t
            JOIN "{schema}".routes r USING (route_id)
            {where}
            GROUP BY 1, 2, 3, 4, 5
        ''', params)
        return {row[:5]: row[5] for row in cur.fetchall()}


def fetch_activity(conn, schema: str, pattern_ids, date_from, date_to):
    """Returns {pattern_id: {date: n_trips}} from pattern_days, within the window."""
    if not pattern_ids:
        return {}
    conditions, params = date_conditions(date_from, date_to)
    conditions.insert(0, "pattern_id = ANY(%s)")
    params.insert(0, sorted(pattern_ids))
    with conn.cursor() as cur:
        cur.execute(f'''
            SELECT pattern_id, date, n_trips
            FROM "{schema}".pattern_days
            WHERE {" AND ".join(conditions)}
        ''', params)
        activity = defaultdict(dict)
        for pattern_id, day, n_trips in cur.fetchall():
            activity[pattern_id][day] = n_trips
    return activity


def check(triplets, stop_info, agency_group, corridors, route_corridors):
    in_area = lambda s: s is not None and stop_info.get(s, (None, False))[1]

    known_cache = {}

    def known_stops(scope, scope_corridors):
        if scope not in known_cache:
            known_cache[scope] = set().union(*(c["index"] for c in scope_corridors))
        return known_cache[scope]

    per_group = defaultdict(lambda: {
        "checked": 0, "consistent": 0, "n_conflicting": 0, "n_unknown": 0, "skipped": 0,
        "conflicts": defaultdict(lambda: [set(), set()]),  # (triplet, scope) -> [routes, pattern_ids]
        "unknown": defaultdict(lambda: [set(), set()]),    # (stop_id, scope) -> [routes, pattern_ids]
        "served_group": set(),
        "served_route": defaultdict(set),
        "routes_seen": set(),
    })
    unmapped_agencies = defaultdict(int)
    groups_without_corridors = set()

    for (agency_id, route, prev_id, stop_id, next_id), pattern_ids in triplets.items():
        group = agency_group.get(agency_id)
        if group is None:
            unmapped_agencies[agency_id] += 1
            continue
        if group not in corridors:
            groups_without_corridors.add(group)
            continue
        g = per_group[group]
        g["routes_seen"].add(route)
        if not in_area(stop_id):
            g["skipped"] += 1
            continue

        key = (prev_id if in_area(prev_id) else None, stop_id, next_id if in_area(next_id) else None)
        stops = [s for s in key if s is not None]
        g["checked"] += 1
        g["served_group"].add(stop_id)
        g["served_route"][route].add(stop_id)

        if (group, route) in route_corridors:
            scope, scope_corridors = f"route {route}", route_corridors[(group, route)]
        else:
            scope, scope_corridors = "group", corridors[group]

        known = known_stops((group, scope), scope_corridors)
        unknown = [s for s in stops if s not in known]
        if unknown:
            g["n_unknown"] += 1
            for s in unknown:
                entry = g["unknown"][(s, scope)]
                entry[0].add(route)
                entry[1].update(pattern_ids)
        elif is_consistent(stops, scope_corridors):
            g["consistent"] += 1
        else:
            g["n_conflicting"] += 1
            entry = g["conflicts"][(key, scope)]
            entry[0].add(route)
            entry[1].update(pattern_ids)

    return per_group, unmapped_agencies, groups_without_corridors


def build_findings(per_group, stop_info):
    """One dict per printed conflict / unknown stop, numbered in print order."""
    name = lambda s: None if s is None else stop_info[s][0]
    findings = []
    for group, g in per_group.items():
        for (key, scope), (routes, pattern_ids) in g["conflicts"].items():
            findings.append({"kind": "conflict", "group": group, "scope": scope,
                             "routes": sorted(routes),
                             "names": tuple(name(s) for s in key),
                             "pattern_ids": sorted(pattern_ids)})
        for (stop_id, scope), (routes, pattern_ids) in g["unknown"].items():
            findings.append({"kind": "unknown_stop", "group": group, "scope": scope,
                             "routes": sorted(routes),
                             "names": (None, name(stop_id), None),
                             "pattern_ids": sorted(pattern_ids)})
    findings.sort(key=lambda f: (f["group"], f["kind"], f["scope"],
                                 tuple(n or "" for n in f["names"])))
    for number, f in enumerate(findings, start=1):
        f["id"] = number
    return findings


def activity_text(pattern_ids, activity):
    """When the given patterns run, summed over the patterns."""
    per_day = defaultdict(int)
    for pattern_id in pattern_ids:
        for day, n_trips in activity.get(pattern_id, {}).items():
            per_day[day] += n_trips
    if not per_day:
        return "no service days in window"
    days = sorted(per_day)
    return (f"{days[0]}..{days[-1]}, {len(days)} days, "
            f"median {median(per_day.values()):g} trips/day")


def format_ids(pattern_ids, limit=10):
    shown = ", ".join(str(i) for i in pattern_ids[:limit])
    return shown + (f" +{len(pattern_ids) - limit} more" if len(pattern_ids) > limit else "")


def print_report(per_group, findings, activity, unmapped_agencies, groups_without_corridors,
                 corridors, route_corridors, name_to_ids):
    for group in sorted(corridors):
        g = per_group[group]
        print(f"\n=== {group} ({', '.join(c['name'] for c in corridors[group])}) ===")
        print(f"  {g['checked']} route triplets checked: {g['consistent']} consistent, "
              f"{g['n_conflicting']} conflicting, {g['n_unknown']} with unknown stops; "
              f"{g['skipped']} skipped (centre outside area)")

        missing_routes = sorted(r for (grp, r) in route_corridors
                                if grp == group and r not in g["routes_seen"])
        if missing_routes:
            print(f"  Listed routes with no triplets: {', '.join(missing_routes)}")

        unknown_findings = [f for f in findings if f["group"] == group and f["kind"] == "unknown_stop"]
        if unknown_findings:
            print("  Unknown stops (served, but on no corridor in scope):")
            for f in unknown_findings:
                print(f"    #{f['id']} [{f['scope']}] {f['names'][1]}  ({', '.join(f['routes'])})")

        conflict_findings = [f for f in findings if f["group"] == group and f["kind"] == "conflict"]
        if conflict_findings:
            print("  Conflicts (no corridor in scope orders these stops):")
            for f in conflict_findings:
                triplet = " -> ".join("∅" if n is None else n for n in f["names"])
                print(f"    #{f['id']} [{f['scope']}] {triplet}  ({', '.join(f['routes'])})  "
                      f"{activity_text(f['pattern_ids'], activity)}; "
                      f"pattern_id: {format_ids(f['pattern_ids'])}")

        missing = [(c["name"], stop_name) for c in corridors[group] for stop_name in c["not_in_feed"]]
        if missing:
            print("  Corridor stops not in feed (could be closed/renamed station or typo):")
            for corridor_name, stop_name in missing:
                print(f"    {corridor_name}: {stop_name}")

        unserved = []
        for c in corridors[group]:
            if c["routes"]:
                served = set().union(*(g["served_route"][r] for r in c["routes"]))
            else:
                served = g["served_group"]
            unserved += [(c["name"], stop_name) for stop_name in c["stops"]
                         if stop_name not in c["not_in_feed"]
                         and not any(s in served for s in name_to_ids[stop_name])]
        if unserved:
            print("  Unserved corridor stops (on the map, not served by its routes/group):")
            for corridor_name, stop_name in unserved:
                print(f"    {corridor_name}: {stop_name}")

    if groups_without_corridors:
        print(f"\nGroups without corridors (skipped): {', '.join(sorted(groups_without_corridors))}")
    if unmapped_agencies:
        print("Agencies in no group (not checked): "
              + ", ".join(f"{a} ({n} triplets)" for a, n in sorted(unmapped_agencies.items())))


def save_findings(conn, schema: str, findings, corridors_file, date_from, date_to):
    """Replace "{schema}".map_check_findings: one row per finding and pattern_id."""
    rows = [(f["id"], f["kind"], f["group"], f["scope"], ", ".join(f["routes"]),
             f["names"][0], f["names"][1], f["names"][2], pattern_id,
             str(corridors_file), date_from, date_to)
            for f in findings for pattern_id in f["pattern_ids"]]

    with conn.cursor() as cur:
        cur.execute(f'DROP TABLE IF EXISTS "{schema}".map_check_findings')
        cur.execute(f'''
            CREATE TABLE "{schema}".map_check_findings (
                finding_id integer NOT NULL,
                kind text NOT NULL,
                group_name text,
                scope text,
                routes text,
                prev_stop_name text,
                stop_name text,
                next_stop_name text,
                pattern_id bigint NOT NULL,
                checked_at timestamptz NOT NULL DEFAULT now(),
                corridors_file text,
                window_from date,
                window_to date
            )
        ''')
        if rows:
            execute_values(cur, f'''
                INSERT INTO "{schema}".map_check_findings (
                    finding_id, kind, group_name, scope, routes,
                    prev_stop_name, stop_name, next_stop_name, pattern_id,
                    corridors_file, window_from, window_to
                ) VALUES %s
            ''', rows)
        cur.execute(f'CREATE INDEX ON "{schema}".map_check_findings (pattern_id)')
    conn.commit()
    return len(rows)


def main():
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--schema", required=True, help="Snapshot schema, e.g. gtfs_20261019")
    parser.add_argument("--corridors", type=Path, default=Path("config/map_corridors.yml"))
    parser.add_argument("--from", dest="date_from", help="Only patterns running on or after "
                        "this date, YYYY-MM-DD (default: whole feed)")
    parser.add_argument("--to", dest="date_to", help="Only patterns running on or before "
                        "this date, YYYY-MM-DD (default: whole feed)")
    parser.add_argument("--no-save", action="store_true",
                        help="Print only; don't write map_check_findings")
    add_connection_args(parser)
    args = parser.parse_args()

    date_from = date.fromisoformat(args.date_from) if args.date_from else None
    date_to = date.fromisoformat(args.date_to) if args.date_to else None
    if date_from and date_to and date_from > date_to:
        parser.error("--from must not be later than --to")

    conn = connect_from_args(args)
    require_table(conn, args.schema, "stop_triplets",
                  f"python py/derive_stop_triplets.py --schema {args.schema}")
    require_table(conn, args.schema, "pattern_days",
                  f"python py/derive_pattern_days.py --schema {args.schema}")

    stop_info, name_to_ids = fetch_stops(conn, args.schema)
    agency_group, corridors, route_corridors = load_corridors(args.corridors, name_to_ids)
    triplets = fetch_triplets(conn, args.schema, date_from, date_to)

    per_group, unmapped_agencies, groups_without_corridors = check(
        triplets, stop_info, agency_group, corridors, route_corridors)
    findings = build_findings(per_group, stop_info)
    conflict_pattern_ids = {pid for f in findings if f["kind"] == "conflict"
                            for pid in f["pattern_ids"]}
    activity = fetch_activity(conn, args.schema, conflict_pattern_ids, date_from, date_to)

    window = ("whole feed" if not (date_from or date_to)
              else f"patterns running {date_from or '...'} .. {date_to or '...'}")
    print(f'Schema "{args.schema}", {window}')
    print_report(per_group, findings, activity, unmapped_agencies, groups_without_corridors,
                 corridors, route_corridors, name_to_ids)

    if not args.no_save:
        n_rows = save_findings(conn, args.schema, findings, args.corridors, date_from, date_to)
        print(f'\nSaved {len(findings)} finding(s) ({n_rows} rows) to '
              f'"{args.schema}".map_check_findings')
    conn.close()


if __name__ == "__main__":
    main()
