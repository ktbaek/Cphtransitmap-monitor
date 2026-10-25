# Copenhagen rail network monitoring from Rejseplanen GTFS

This project detects changes to the Copenhagen-area rail network using a [GTFS](https://gtfs.org) static feed from [Rejseplanen Labs](https://labs.rejseplanen.dk/). The goal is to maintain and update the customer-facing map [Copenhagen Transit Map](https://cphtransitmap.dk/en). The project contains tools that detect new/closed stations, changed routing, and changes in service pattern.

## Status
- **Map version:** see `mapversion` in `config/map_*.yml.`
- **Map last synced to snapshot:** `gtfs_20261019`
- **Feed ends:** `2027-01-13`
- **Map verified until:** `2026-12-12` (earliest last day among the agencies on the map; see [step 3](#3-check-what-the-feed-covers))

## Data
Access to the data requires authorization from Rejseplanen Labs. The GTFS data is not included in this repository and is subject to Rejseplanen Labs' terms. Unzipped feeds go in `data/`.

## Setup
Python 3.14 and PostgreSQL 17.9 (no PostGIS needed). 

```bash
python -m venv .venv
source .venv/bin/activate   # Windows: .venv\Scripts\activate
pip install -r requirements.txt
```

The R scripts in `R/` (optional, used for plots in steps 3 and 5) need R with `tidyverse`, `glue`, `DBI`, `RPostgres` and `yaml`. Open `cphtransitmap-monitor.Rproj` or run them from the project root.

Connection settings go in `config/db.yml` (block `default`; password optional, falls back to `PGPASSWORD`). The database must already exist.

```yaml
default:
  host: localhost
  port: 5432
  dbname: <gtfs>
  user: <user>
```

Area polygons live in `config/` as GeoJSON files.

## When a new feed arrives

Run all commands from the project root (scripts read `config/` relative to it); the Python scripts live in `py/`. Dates: `YYYY-MM-DD` in the feed folder name, `--snapshot-date`, `--week`, `--from` and `--to`; `YYYYMMDD` in the schema name (`gtfs_YYYYMMDD`). Replace them with the dates that apply.

### 1. Get and unpack the feed
Download the GTFS zip from Rejseplanen Labs (requires authorization, see [Data](#data)) and unzip it into `<data/GTFS-YYYY-MM-DD/>`. Check that the expected `.txt` files are present.

### 2. Load and derive
```bash
python py/load_gtfs.py --gtfs-dir data/GTFS-YYYY-MM-DD --snapshot-date YYYY-MM-DD
python py/create_views.py --schema gtfs_YYYYMMDD
python py/derive_service_days.py --schema gtfs_YYYYMMDD
python py/derive_trip_patterns.py --schema gtfs_YYYYMMDD
python py/derive_stop_triplets.py --schema gtfs_YYYYMMDD
python py/derive_pattern_days.py --schema gtfs_YYYYMMDD
python py/derive_service_pattern.py --schema gtfs_YYYYMMDD --week YYYY-MM-DD
```

`--week` is the Monday of the week to derive; repeat it for several weeks, or use `--all-weeks` (see [Weeks](#weeks)). Pick weeks without public holidays.

Each script needs the ones above it, with two exceptions: `derive_service_pattern.py` only needs `derive_service_days.py`, and `derive_stop_triplets.py` and `derive_pattern_days.py` only need `derive_trip_patterns.py` (either order). A script stops with a message naming the missing step if a prerequisite table doesn't exist. If you re-run `derive_trip_patterns.py`, re-run `derive_stop_triplets.py`, `derive_pattern_days.py` and the corridor check too, because `pattern_id`s change.

`load_gtfs.py` refuses to overwrite an existing schema. Use `--force` only if you want to drop and reload that snapshot.

### 3. Check what the feed covers
A feed spans a couple of months and can contain a timetable change, and agencies may end at different dates. Check the overall range, and trips per day per agency:

```sql
SELECT min(date) AS first_day, max(date) AS last_day
FROM gtfs_<new>.service_days;

SELECT a.agency_name, min(pd.date) AS first_day, max(pd.date) AS last_day
FROM gtfs_<new>.pattern_days pd
JOIN gtfs_<new>.trip_patterns tp USING (pattern_id)
JOIN gtfs_<new>.routes r ON r.route_id = tp.route_id
JOIN gtfs_<new>.agency a ON a.agency_id = r.agency_id
GROUP BY 1
ORDER BY 3;

SELECT pd.date, a.agency_name, sum(pd.n_trips) AS n_trips
FROM gtfs_<new>.pattern_days pd
JOIN gtfs_<new>.trip_patterns tp USING (pattern_id)
JOIN gtfs_<new>.routes r ON r.route_id = tp.route_id
JOIN gtfs_<new>.agency a ON a.agency_id = r.agency_id
GROUP BY 1, 2
ORDER BY 2, 1;
```
The first query gives *Feed ends* for the [Status](#status). The second gives each agency's last day; *Map verified until* is the earliest of these among the agencies on the map (those in `map_corridors.yml`). Beyond that date, checks for that agency are missing data, so re-fetch when its next timetable is published.

Plot the third query (one panel per agency) with [`R/plot_calendar.R`](R/plot_calendar.R). Set `gtfs_schema` at the top of the script first. It writes `output/agency_trips_<schema>.png`, and also `output/calendar_expections_<schema>.png`, which shows each rail service's `calendar_dates` additions and removals. Look for:
- **An agency that stops early.** Its next timetable isn't in this feed yet, so plan to re-fetch once it is published. Checks against the map for the later weeks would show its corridors as unserved, which is missing data and not a closure.
- **A step change** (e.g. mid-December): a timetable change. Run the checks separately for the periods before and after it (`--from`/`--to`, and one `--week` per period).
- **Dips** on public holidays. Avoid those weeks for service patterns.

### 4. Check against the map
```bash
python py/check_map_corridors.py --schema gtfs_YYYYMMDD
python py/check_map_corridors.py --schema gtfs_YYYYMMDD --from YYYY-MM-DD --to YYYY-MM-DD
python py/check_map_service_patterns.py --schema gtfs_YYYYMMDD --week YYYY-MM-DD
```
The corridor check covers the whole feed unless you give a window, and prints one numbered finding per line, with when the conflicting patterns run. It also saves the findings (see [Findings](#findings)). The service pattern check covers one week and only prints. Read the output for:
- **Corridor stops not in feed**: a stop name in `map_corridors.yml` that isn't in the feed's `rail_stops`. Could be a closed or renamed station, or a typo in the corridor file. These stops are left out of the corridor's checks, so the other findings below still run.
- **Unknown stops**: served, but on no map corridor in scope. Usually a new or renamed station. A rename often also appears under "Corridor stops not in feed" with the old name.
- **Conflicts**: a triplet the map's corridors can't explain, meaning routing has changed, the route is a temporary diversion, or the map is wrong. See [step 5](#5-judge-the-findings).
- **Unserved corridor stops**: on the map and in the feed, but not served by the corridor's routes, or by the group if it lists none, in the period checked. Usually a station that has lost service but still exists in the feed, or a route that no longer stops there.
- **Listed routes with no triplets**: usually a typo, a renamed route_short_name, or a route that's been discontinued.
- **Service category mismatches**: a change in frequency pattern for a stop, in the chosen week.

*Known false positives*: Høvelte St., early morning trips of S-tog route H from Frederikssund St., afternoon trips of S-tog route F to Klampenborg St. See [Gotchas](#gotchas).

### 5. Judge the findings
Most conflicts are temporary: diversions and track work around København Syd are typical. What separates them from a real change is when they run, so plot the findings against `pattern_days` (the saved `map_check_findings` table joins to it on `pattern_id`). [`R/plot_conflicts.R`](R/plot_conflicts.R) does this: set `gtfs_schema` at the top, and it writes `output/conflicts_<schema>.png` with one row per finding and a dot per day the patterns run (dot size is `n_trips`), for the whole feed. The plot's x-axis is limited to the range of `service_days`. The query behind it is:

```sql
SELECT f.finding_id, f.stop_name, pd.date, pd.pattern_id, pd.n_trips
FROM gtfs_<new>.map_check_findings f
JOIN gtfs_<new>.pattern_days pd USING (pattern_id)
WHERE f.kind = 'conflict';
```
- **A few days, then gone**: a diversion. No change to the map.
- **Runs through to the end of the feed, or from some date onwards**: a real change (a timetable change or a new routing). The map, or the corridor file, needs to follow.
- **Every day for the whole period**: the map or the corridor file is wrong.

### 6. Update the map and record the result
1. Create a branch for this snapshot: `git switch -c sync-gtfs_YYYYMMDD`. Until you merge it, `main` still describes the published map.
2. Decide per finding whether the feed or the map is right.
3. Edit `config/map_corridors.yml` and `config/map_service_patterns.yml` to describe the map as it *will* look after the update (see [Config files](#config-files)). Re-run [step 4](#4-check-against-the-map) until every remaining finding is one you have judged to be temporary. `git diff main` on the two files is now the list of changes to make on the map.
4. Update the map, and check that it matches the YAML.
5. When the updated map is published, bump `mapversion` in the YAML files, and update the [Status](#status) line, then commit: `git commit -am "Sync map to gtfs_YYYYMMDD (mapversion X.Y.Z)"`.
6. Switch to main (`git switch main`), merge the branch (`git merge sync-gtfs_YYYYMMDD`) and then delete it (`git branch -d sync-gtfs_YYYYMMDD`). 

## Pipeline
Each snapshot lives in its own Postgres schema, named `gtfs_YYYYMMDD`. Raw tables are loaded with `COPY` and every column is stored as `text`; cast explicitly when needed (see [Gotchas](#gotchas)).
1. **Raw tables** hold every row from the feed as-is. Bus, ferry, and long-distance rail outside the area are all still there.
2. **`rail_*` views** (`create_views.py`) narrow to rail route types only, with no area restriction yet. They are `rail_routes`, `rail_trips`, `rail_stop_times` and `rail_stops`. Which services actually run, and when, comes from `service_days`, not from these views.
3. **`stops.in_cph_area`** is a flag, not a filter: a stop is inside if its coordinates fall in the polygon `config/cph_area.geojson` (`shapely` point-in-polygon).
4. **`service_days`** (`derive_service_days.py`) expands `calendar.txt` and `calendar_dates.txt` into one row per service and date on which it runs. See [Service days](#service-days).
5. **Topology tables** (`trip_patterns`, `stop_triplets`, `pattern_days`) cover the whole feed. Pass-through stops are dropped, services that never run are dropped, and only patterns that touch the Copenhagen area are kept. When each pattern runs is in `pattern_days`.
6. **Service pattern tables** (`stop_service_pattern`, view `service_pattern`) are derived per chosen week, because the map shows service as a weekly pattern.

## Concepts

### Service days
`service_days` has one row per `(service_id, date)` on which a service runs, with `isodow` (1 = Monday to 7 = Sunday). A service runs on a date if the date is inside the `calendar` validity period and its weekday flag is set, or `calendar_dates` adds it (`exception_type` 1), unless `calendar_dates` removes it (`exception_type` 2). Services defined only through `calendar_dates` are included. That matters because the feed encodes some regular service that way (e.g. all of Metroselskabet's), and new timetables starting mid-feed, which a weekly-calendar filter would drop. The table covers every service used by a rail trip, for the whole feed.

### Weeks
A week is Monday to Sunday, identified by the date of its Monday (`week_start`). Only `stop_service_pattern` and `service_pattern` are derived per week. A week must lie fully inside the range of `service_days`, and a service counts for it if it runs on at least `--min-days` days of it (default 1). Day types come from the actual dates (`isodow`): a trip is a weekday trip if its service runs on a Monday to Friday of the week, a Saturday trip if it runs on the Saturday, and a Sunday/holiday trip if it runs on the Sunday. Public holidays are not treated specially, so use weeks without them.

### Trip pattern
A trip pattern is one distinct ordered sequence of stops that trips on a route run in one direction. `trip_patterns` has one row per pattern, with the stops (ids, names, in-area flags) as arrays in visiting order, plus the trips that run it. In other words, a trip pattern is composed of all trips that are identical with regard to their stop sequence, direction and `route_id`.

Patterns cover the whole feed. A trip is included if its service runs on at least one day in `service_days`, so short-lived variants such as diversions are included alongside permanent patterns; `pattern_days` tells them apart. Pass-through stops (`pickup_type` and `drop_off_type` both `1`) and patterns that never touch the Copenhagen area are left out. Trips that overlap the area are kept in full, with `stop_in_cph_area` marking which stops are in scope. `pattern_id` is just a row identifier within one snapshot. It can't be used to match patterns between snapshots, and it changes if `trip_patterns` is re-derived.

### Pattern days
`pattern_days` has one row per `(pattern_id, date)` on which at least one trip of the pattern runs, with the number of trips that day (`n_trips`). It is the tool for judging how regular a pattern is: plot `n_trips` per date to tell a one-week diversion from a permanent routing, or to see how a regular route changes at a timetable change.

### Stop triplets
Triplets (prev, stop, next) are the unit for map checks. `stop_triplets` has one row per (pattern, position), each attributed to the route it comes from. `pattern_id` stays on each row, so any suspicious triplet can be traced back to its full pattern in `trip_patterns`, and to when it runs in `pattern_days`. Termini and out-of-area neighbours are NULL wildcards, so line ends are checked more loosely.
 
A map check can ask whether each triplet's stops sit in order on some corridor, with no need to split patterns at junctions. Unlike pairwise edges, triplets also keep which through-movements exist at a junction (lines A-X-C and B-X-D meeting at X do not imply A-X-B).

### Corridor
A corridor is one line as drawn on the map, written out in `config/map_corridors.yml` as its complete stop list in order, at maximum length. 

A triplet is consistent if its stops appear in order on one corridor, in either direction. Corridors belong to a group (`metro`, `stog`, `dsb`, `lokaltog`, `letbane`, defined by GTFS agency IDs) and may list the `routes` (by `route_short_name`) that run on them. A triplet is checked against its route's own corridors if the route is listed, otherwise against all corridors in its group. Stops are matched by exact `stop_name` (metro stations end in "(Metro)"), so a station renamed in the feed shows up as an unknown stop. A line that loops (e.g. M3) sets `ring: true`. Corridors may overlap: M1 and M2 each list the shared trunk from Vanløse to Christianshavn, because they are separate lines on the map.

### Findings
`check_map_corridors.py` saves what it reports (conflicts and unknown stops) to `map_check_findings`, one row per finding and `pattern_id`. `finding_id` matches the `#number` on each printed line. The table is replaced on every run, and records when it ran, which corridor file and which window. Join it to `pattern_days` to plot when the patterns behind a finding run.

## Scripts

### Loading and deriving
These scripts load the relevant GTFS files into PostgreSQL tables and derive service days, trip patterns, stop triplets, pattern days and service patterns.

#### `gtfs_common.py`
Shared DB connection helpers (`--dsn`, `--config`, `--config-env`).

#### `gtfs_weeks.py`
Shared week helpers (validating `--week` against `service_days`, listing full weeks, choosing a week from a derived table) and `require_table`, which the derive and check scripts use to stop with a pointer to the missing step.

#### `load_gtfs.py`
Loads raw tables (all columns as `text`) and adds the boolean `in_cph_area` to the `stops` table (see [Pipeline](#pipeline)).

  | Option | Default | Description |
  | :--- | :--- | :--- |
  | `--gtfs-dir` | *Required* | Path to the unzipped GTFS feed directory |
  | `--snapshot-date` | *Required* | e.g. `2026-09-21` |
  | `--polygon` | `config/cph_area.geojson` | Path to Copenhagen area polygon |
  | `--force` | *(flag)* | Overwrite the snapshot's schema if it already exists. Without this flag, the script refuses to run again for a date that's already been loaded. |

#### `create_views.py`
Creates/replaces the rail views (`rail_routes`, `rail_trips`, `rail_stop_times`, `rail_stops`). Rail route types come from `RAIL_ROUTE_TYPES`, which covers basic and extended GTFS codes.

  | Option | Default | Description |
  | :--- | :--- | :--- |
  | `--schema` | *Required* | Snapshot schema, e.g. `gtfs_20260921` |

#### `derive_service_days.py`
Creates table `service_days` (`service_id`, `date`, `isodow`) for the whole feed. See [Service days](#service-days). Prints the row count and date range, and warns about rail services with no service day at all.

  | Option | Default | Description |
  | :--- | :--- | :--- |
  | `--schema` | *Required* | Snapshot schema, e.g. `gtfs_20260921` |

#### `derive_trip_patterns.py`
Creates table `trip_patterns`, one row per distinct ordered stop sequence per route/direction, for the whole feed. Columns: `pattern_id`, `route_id`, `route_short_name`, `direction_id`, `first_stop`, `last_stop`, `stop_ids`, `stop_names`, `stop_in_cph_area` (arrays in visiting order), `n_trips`, `trip_ids`. See [Trip pattern](#trip-pattern) for what is kept and left out.

  | Option | Default | Description |
  | :--- | :--- | :--- |
  | `--schema` | *Required* | Snapshot schema, e.g. `gtfs_20260921` |

#### `derive_stop_triplets.py`
Creates table `stop_triplets` (prev, stop, next), one row per (pattern, position), attributed to the route it comes from. Columns: `route_id`, `route_short_name`, `direction_id`, `pattern_id`, `n_trips`, `prev_stop_id`, `stop_id`, `next_stop_id`. See [Stop triplets](#stop-triplets).

  | Option | Default | Description |
  | :--- | :--- | :--- |
  | `--schema` | *Required* | Snapshot schema, e.g. `gtfs_20260921` |

#### `derive_pattern_days.py`
Creates table `pattern_days` (`pattern_id`, `date`, `isodow`, `n_trips`) for all patterns and the whole feed. See [Pattern days](#pattern-days).

  | Option | Default | Description |
  | :--- | :--- | :--- |
  | `--schema` | *Required* | Snapshot schema, e.g. `gtfs_20260921` |

#### `derive_service_pattern.py`
Creates presence table `stop_service_pattern` (`week_start, route_id, stop_id, day_type, time_band`) for the chosen week(s), based on the days a service runs in that week. Pass-through stops are excluded as in the trip patterns. Weekday bands are set by constants at the top of the script, which are the source of truth for the hours below. Saturday and Sunday are not split by time of day and are tested together. Re-running a week replaces its rows; other weeks are left alone. See [Weeks](#weeks).
 
Also creates view `service_pattern` (one row per week), collapsing the presence table into one named `service_category` per (week_start, route_id, stop_id):
 
  | `service_category` | Weekday peak | Weekday daytime | Weekday evening | Sat/Sun |
  | :--- | :-: | :-: | :-: | :-: |
  | `all_times` | ✓ | ✓ | ✓ | ✓ |
  | `weekday_only` | ✓ | ✓ | ✓ | – |
  | `weekday_daytime_only` | ✓ | ✓ | – | – |
  | `peak_only` | ✓ | – | – | – |
  | `weekend_and_daytime_only` | ✓ | ✓ | – | ✓ |
  | `weekend_and_evening_only` | – | – | ✓ | ✓ |
  | `weekend_only` | – | – | – | ✓ |
 
The bands are the windows the script tests, not full coverage of the day: peak 06–09 and 14–17, daytime 11–13, evening 21–24. Because Saturday and Sunday are tested together there is no Saturday-only or Sunday-only category. Any combination that doesn't match a row above falls back to a raw `+`-joined list of the bands it was actually seen in.

  | Option | Default | Description |
  | :--- | :--- | :--- |
  | `--schema` | *Required* | Snapshot schema, e.g. `gtfs_20260921` |
  | `--week` | first full week of the feed | Monday of a week to derive, `YYYY-MM-DD`; repeat the flag for several weeks |
  | `--all-weeks` | *(flag)* | Derive every full week inside the feed's service days (instead of `--week`) |
  | `--min-days` | `1` | A service counts for a week if it runs on at least this many days of it |

### Visualization helpers
These scripts generate GeoJSON files of the shapes in the GTFS data, for a visual overview. They are not required for any analysis.

#### `export_geojson.py`
Shapes + stops per agency, with far-away geometry grouped onto
`config/destination_*.geojson` points to keep files readable. Only trips whose service has at least one day in `service_days` are used, so it requires `derive_service_days.py`.

  | Option | Default | Description |
  | :--- | :--- | :--- |
  | `--schema` | *Required* | Snapshot schema, e.g. `gtfs_20260921` |
  | `--agency` | *Required* | Agency id to export; repeat the flag for multiple agencies |
  | `--out-dir` | `output` | Output directory |
  | `--polygon` | `config/cph_area.geojson` | Path to Copenhagen area polygon |
  | `--destinations-dir` | `config` | Directory containing `destination_*.geojson` polygons |
  | `--destination-loc` | `config/destination_loc.txt` | Replacement coordinates for each destination polygon |
  | `--no-group` | *(flag)* | Disable collapsing of out-of-area geometry onto destination points |

#### `export_shape.py`
A single `shape_id`, no joins, for quick spot checks.

  | Option | Default | Description |
  | :--- | :--- | :--- |
  | `--schema` | *Required* | Snapshot schema, e.g. `gtfs_20260921` |
  | `--shape` | *Required* | `shape_id` to export |
  | `--out-dir` | `output` | Output directory |

See `output/` for example exports.

### Checking against the current map
These scripts check whether the topology and service patterns shown on the map are consistent with the GTFS data, using the hand-maintained files described under [Config files](#config-files).

#### `check_map_corridors.py`
Checks `stop_triplets` against the corridors in `config/map_corridors.yml` (matching rules under [Corridor](#corridor)). Reports corridor stops not in the feed, unknown stops, conflicts, corridor stops not served by the corridor's routes, and listed routes with no triplets. Every conflict line gets a `#number`, shows when its patterns run (first and last day, number of days, median trips per day, from `pattern_days`) and lists their `pattern_id`s. The findings are saved to `map_check_findings` (see [Findings](#findings)); this is the only thing the script writes.

  | Option | Default | Description |
  | :--- | :--- | :--- |
  | `--schema` | *Required* | Snapshot schema, e.g. `gtfs_20260921` |
  | `--corridors` | `config/map_corridors.yml` | Path to the corridor definitions |
  | `--from` | *(none)* | Only check patterns that run on or after this date, `YYYY-MM-DD`; also limits the activity shown |
  | `--to` | *(none)* | Only check patterns that run on or before this date, `YYYY-MM-DD` |
  | `--no-save` | *(flag)* | Print only; don't write `map_check_findings` |

Without `--from`/`--to` the whole feed is checked, so "unserved corridor stops" means not served anywhere in the feed.

#### `check_map_service_patterns.py`
Print-only. Checks view `service_pattern` for one week against `config/map_service_patterns.yml`. Every route of every `agencies:` entry is checked (S-tog/Metro), whether or not it's mentioned under `routes:`, so an unlisted route with a real deviation isn't silently skipped. Reports category mismatches and map exceptions for stops the route doesn't serve.

  | Option | Default | Description |
  | :--- | :--- | :--- |
  | `--schema` | *Required* | Snapshot schema, e.g. `gtfs_20260921` |
  | `--week` | the only week in `stop_service_pattern` | Monday of the week to check, `YYYY-MM-DD`; must have been derived. Required if several weeks exist |
  | `--patterns` | `config/map_service_patterns.yml` | Path to the service pattern exceptions |

### Common options
Every script above also accepts these, defined once in `gtfs_common.py` and resolved into a
connection by `connect_from_args()`. You normally don't need to pass any of them: with nothing set, every script reads the `default` block of `config/db.yml`. They exist so a single run can be pointed elsewhere — e.g. a different environment via `--config-env`, or a different database entirely via `--dsn`.

| Option | Default | Description |
| :--- | :--- | :--- |
| `--dsn` | `$GTFS_DB_DSN` | PostgreSQL connection string; overrides `--config` if set |
| `--config` | `config/db.yml` | Path to config file |
| `--config-env` | `default` | Top-level key in `db.yml` to use |


## Config files
`config/map_corridors.yml` and `config/map_service_patterns.yml` are manual encodings of what's currently drawn on the map. They are not generated from the GTFS data, and they are the only record of what the map currently draws, so edit them whenever the map changes.
 
**`map_corridors.yml`** currently encodes the whole rail network: Metro (M1-M4), all S-tog lines, the DSB regional corridors (Kystbanen, Øresundsbanen, Vestbanen, and others), every Lokaltog line, and Letbanen. Stop names must match `rail_stops.stop_name` exactly, although runs of whitespace in either the YAML file or the GTFS feed are tolerated. Excerpt:
 
```yaml
groups:
  metro:    {agencies: ['319']}
  stog:     {agencies: ['318']}
  dsb:      {agencies: ['203', '471', '401', '451']}
  lokaltog: {agencies: ['316']}
  letbane:  {agencies: ['481']}
 
corridors:
  M1:
    group: metro
    routes: [M1]
    stops:
      - Vanløse St. (Metro)
      - Flintholm St. (Metro)
      # ...
```
 
**`map_service_patterns.yml`** is a sparse per-route exception list: every stop a route serves is assumed `default: all_times` unless listed. Excerpt:
 
```yaml
default: all_times

agencies:
  - '318'  # DSB S-tog
  - '319'  # Metroselskabet

routes:
  A:
    Virum St.: weekend_and_evening_only
    Sorgenfri St.: weekend_and_evening_only
    # ...
```

## Gotchas
- **Stop name signifiers.** Stop names in the feed may add signifiers, e.g. "(Metro)" as in "Nørreport St. (Metro)", or "(Hillerød)" for some Lokaltog stops as in "Kagerup St. (Hillerød)".
- **Text columns.** Every raw column is `text`, so cast before comparing or sorting (`stop_sequence::int`, `route_type::int`, `stop_lat::float`). Ordering by `stop_sequence` without the cast sorts lexicographically and scrambles patterns with 10+ stops (already fixed in `derive_trip_patterns.py`). `service_days.service_id` is `text` like the raw tables, but `service_days.date` is a real `date`.
- **A feed holds more than one network.** It can contain a timetable change, diversions and holiday timetables, and agencies can end at different dates. Whole-feed results mix all of these, so read them together with `pattern_days`, and use `--from`/`--to` or `--week` to look at one period.
- **`pattern_id`s are short-lived.** They are only valid for the current `trip_patterns`, so re-run the downstream steps and the corridor check after re-deriving it. Don't store them.
- **Holidays.** `calendar_dates.txt` is modelled in `service_days`, but public holidays are not a separate day type, so a holiday inside a week distorts that week's service categories. Pick weeks without holidays.
- **Høvelte St.** appears in the feed as a stop but is intentionally not shown on the map.
- **Shapes.** When visually inspecting routes, note that not all shapes in the feed's `shapes.txt` provide geographical detail between stops.

## To do
- `transfers.txt` is loaded (`load_gtfs.py`'s `GTFS_FILES`) into a raw `transfers` table, but nothing reads it yet. Could be used to check rail-to-rail interchanges against the map's drawn connections (e.g. Nørreport St. ↔ Nørreport St. (Metro)).
- Treat public holidays as their own day type in the service patterns.
- Other exploratory SQL queries to aid understanding of the network.

## License

The code and configuration in this repository are released under the MIT License (see `LICENSE`). This does not cover the [Copenhagen Transit Map](https://cphtransitmap.dk/en) itself, including its design, data and name, which are not part of this repository. The GTFS data is not included and is subject to Rejseplanen Labs' terms.
