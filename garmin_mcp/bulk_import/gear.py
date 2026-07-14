"""Import gear inventory + gear-to-activity links from the bulk export.

Source: DI_CONNECT/DI-Connect-Fitness/*_gear.json
Structure: `[{"gearDTOS": [...], "gearActivityDTOs": {"<gearPk>": [{"gearPk":..,"activityId":..}, ...]}}]`
"""

from __future__ import annotations

import json
import zipfile
from fnmatch import fnmatch

from garmin_mcp.db.connection import upsert
from garmin_mcp.bulk_import.report import ImportReport

GEAR_GLOB = "DI_CONNECT/DI-Connect-Fitness/*_gear.json"


def import_gear(zf: zipfile.ZipFile, conn, report: ImportReport) -> None:
    result = report.category("gear")
    matched = [n for n in zf.namelist() if fnmatch(n, GEAR_GLOB)]
    result.files_found = len(matched)
    if not matched:
        result.notes.append("no gear file found in export")
        return

    for name in matched:
        with zf.open(name) as f:
            data = json.load(f)
        for wrapper in data:
            gear_items = wrapper.get("gearDTOS", [])
            result.records_seen += len(gear_items)
            for gear in gear_items:
                if "gearPk" not in gear:
                    result.skipped += 1
                    continue
                row = {
                    "gear_id": gear["gearPk"],
                    "uuid": gear.get("uuid"),
                    "gear_type": gear.get("gearTypeName"),
                    "status": gear.get("gearStatusName"),
                    "make_model": gear.get("customMakeModel"),
                    "date_begin": gear.get("dateBegin"),
                    "date_end": gear.get("dateEnd"),
                    "max_distance_m": gear.get("maximumMeters"),
                    "source": "csv_export",
                }
                upsert(conn, "gear", row, ["gear_id"])
                result.rows_written += 1

            links = wrapper.get("gearActivityDTOs", {}) or {}
            for gear_pk, activity_links in links.items():
                for link in activity_links:
                    activity_id = link.get("activityId")
                    if activity_id is None:
                        continue
                    # activity_id may reference an activity not present in
                    # this export (e.g. deleted since) -- FK would reject it.
                    exists = conn.execute(
                        "SELECT 1 FROM activities WHERE activity_id = ?", (activity_id,)
                    ).fetchone()
                    if not exists:
                        result.skipped += 1
                        continue
                    upsert(
                        conn,
                        "activity_gear",
                        {"activity_id": activity_id, "gear_id": int(gear_pk)},
                        ["activity_id", "gear_id"],
                    )
    conn.commit()
