#!/usr/bin/env python3
"""
export_geojson.py

Export rail line shapes and stops for one or more agencies as a single GeoJSON
FeatureCollection, for visual inspection of the network (e.g. in geojson.io or
QGIS). LineString features come from `shapes` (joined to route_id/route_short_name
via rail_trips), Point features from the `rail_stops` view. Only trips whose
service runs on at least one day in service_days are used, so services that
never run are left out (requires derive_service_days.py).

Usage:
    python py/export_geojson.py --schema gtfs_20260921 --agency 203
    python py/export_geojson.py --schema gtfs_20260921 --agency 203 --agency 318
"""

import argparse
import json
from pathlib import Path

from gtfs_common import add_connection_args, connect_from_args
from gtfs_weeks import require_table


def export_lines(conn, schema: str, agency_ids):
    with conn.cursor() as cur:
        cur.execute(
            f'''
            SELECT DISTINCT r.route_id, r.route_short_name, sh.shape_id, a.agency_id
            FROM "{schema}".shapes sh
            JOIN "{schema}".rail_trips t USING (shape_id)
            JOIN "{schema}".routes r USING (route_id)
            JOIN "{schema}".agency a USING (agency_id)
            WHERE a.agency_id = ANY(%s)
              AND t.service_id IN (SELECT service_id FROM "{schema}".service_days)
            ''',
            (agency_ids,),
        )
        shape_routes = cur.fetchall()

    features = []

    with conn.cursor() as cur:
        for route_id, route_short_name, shape_id, agency_id in shape_routes:
            cur.execute(
                f'''
                SELECT shape_pt_lat::float, shape_pt_lon::float
                FROM "{schema}".shapes
                WHERE shape_id = %s
                ORDER BY shape_pt_sequence::int
                ''',
                (shape_id,),
            )
            coords = [[lon, lat] for lat, lon in cur.fetchall()]

            features.append({
                "type": "Feature",
                "geometry": {"type": "LineString", "coordinates": coords},
                "properties": {
                    "shape_id": shape_id,
                    "route_id": route_id,
                    "route_short_name": route_short_name,
                    "agency_id": agency_id,
                },
            })

    print(f"Lines: {len(features)} exported")
    return features


def export_stops(conn, schema: str, agency_ids):
    with conn.cursor() as cur:
        cur.execute(
            f'''
            SELECT s.stop_id, s.stop_name, s.stop_lat::float, s.stop_lon::float,
                   array_agg(DISTINCT r.route_short_name ORDER BY r.route_short_name) AS route_short_names
            FROM "{schema}".rail_stops s
            JOIN "{schema}".rail_stop_times st USING (stop_id)
            JOIN "{schema}".rail_trips t USING (trip_id)
            JOIN "{schema}".routes r USING (route_id)
            JOIN "{schema}".agency a USING (agency_id)
            WHERE a.agency_id = ANY(%s)
              AND t.service_id IN (SELECT service_id FROM "{schema}".service_days)
            GROUP BY s.stop_id, s.stop_name, s.stop_lat, s.stop_lon
            ''',
            (agency_ids,),
        )
        stops = cur.fetchall()

    features = [
        {
            "type": "Feature",
            "geometry": {"type": "Point", "coordinates": [lon, lat]},
            "properties": {
                "stop_id": stop_id,
                "stop_name": stop_name,
                "route_short_names": route_short_names,
            },
        }
        for stop_id, stop_name, lat, lon, route_short_names in stops
    ]

    print(f"Stops: {len(features)} exported")
    return features


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--schema", required=True, help="Snapshot schema, e.g. gtfs_20260921")
    parser.add_argument("--agency", required=True, action="append",
                         help="Agency id to export; repeat for multiple agencies.")
    parser.add_argument("--out-dir", type=Path, default=Path("output"))
    add_connection_args(parser)
    args = parser.parse_args()

    conn = connect_from_args(args)
    require_table(conn, args.schema, "service_days",
                  f"python py/derive_service_days.py --schema {args.schema}")
    line_features = export_lines(conn, args.schema, args.agency)
    stop_features = export_stops(conn, args.schema, args.agency)
    conn.close()

    feature_collection = {
        "type": "FeatureCollection",
        "features": line_features + stop_features,
    }

    args.out_dir.mkdir(parents=True, exist_ok=True)
    out_path = args.out_dir / f"{args.schema}_agency_{'_'.join(args.agency)}.geojson"
    with open(out_path, "w") as f:
        json.dump(feature_collection, f)

    print(f"\nWrote {len(feature_collection['features'])} features to {out_path}")


if __name__ == "__main__":
    main()
