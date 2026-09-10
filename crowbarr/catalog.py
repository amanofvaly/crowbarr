"""Subtitle-library hierarchy and persistent processing preferences.

Manager IDs identify titles across upgrades; folder discovery uses the show directory.
The most specific explicit preference wins: file, season, title, then audio policy.
"""
from __future__ import annotations

import json
import re
import time
from pathlib import Path

EPISODE = re.compile(r"(?i)\bS(\d{1,3})E(\d{1,4})(?:[- .]?E?(\d{2,4}))?")
LANGUAGES = {
    "english": "en", "eng": "en", "hindi": "hi", "hin": "hi",
    "french": "fr", "fra": "fr", "fre": "fr", "german": "de", "deu": "de", "ger": "de",
    "spanish": "es", "spa": "es", "japanese": "ja", "jpn": "ja", "korean": "ko", "kor": "ko",
    "tamil": "ta", "tam": "ta", "telugu": "te", "tel": "te", "malayalam": "ml", "mal": "ml",
    "italian": "it", "ita": "it", "portuguese": "pt", "por": "pt", "russian": "ru", "rus": "ru",
    "chinese": "zh", "zho": "zh", "chi": "zh", "arabic": "ar", "ara": "ar",
}
UNKNOWN = {"", "und", "unknown", "untagged", "mul", "multi", "multiple", "zxx"}


def language_codes(value) -> list[str]:
    if isinstance(value, str):
        value = re.split(r"[/,;|+]", value)
    if not isinstance(value, list):
        return []
    result = []
    for item in value:
        raw = str(item.get("name", "") if isinstance(item, dict) else item).strip().lower()
        code = LANGUAGES.get(raw, raw)
        result.append("und" if code in UNKNOWN or not re.fullmatch(r"[a-z]{2,3}", code) else code)
    return sorted(set(result))


def identity(record: dict) -> dict:
    path = Path(record["path"])
    match = EPISODE.search(path.stem)
    provider = record.get("provider", "folders")
    show = provider == "sonarr" or (provider == "folders" and bool(match))
    season = record.get("season")
    episodes = record.get("episodes") or []
    if show and match:
        if season is None:
            season = int(match[1])
        if not episodes:
            episodes = [int(match[2])]
            if match[3] and int(match[2]) < int(match[3]) <= int(match[2]) + 20:
                episodes = list(range(int(match[2]), int(match[3]) + 1))
    title = record.get("title") or path.stem
    if provider == "folders":
        parent = path.parent
        if show and re.fullmatch(r"(?i)(season[ ._-]*\d+|specials)", parent.name):
            parent = parent.parent
        title = (match.string[:match.start()].strip(" ._-").replace(".", " ") or parent.name) if show else path.stem
        key = "folders:" + str(parent if show else path)
    else:
        key = f"{provider}:{record['item_id']}"
    return {**record, "title": title, "key": key, "kind": "shows" if show else "movies",
            "season": season, "episodes": episodes, "audio_languages": record.get("audio_languages") or []}


def rows(connection) -> list[dict]:
    managed = connection.execute(
        "SELECT m.*,d.metadata,j.id AS job_id,j.state,j.output,j.error,j.stage,"
        "json_extract(j.report,'$.audited_subtitle') AS audited_subtitle,"
        "json_type(j.report,'$.candidate')='text' AS has_candidate,"
        "json_extract(j.report,'$.selected_source_kind') AS selected_source_kind "
        "FROM managed_media m LEFT JOIN library_metadata d ON d.path=m.path "
        "LEFT JOIN jobs j ON j.media=m.path ORDER BY m.provider"
    ).fetchall()
    records, seen = [], set()
    for row in managed:
        record = dict(row)
        if record["path"] in seen:
            continue
        seen.add(record["path"])
        metadata = json.loads(record.pop("metadata") or "{}")
        records.append(identity({**record, **metadata}))
    for row in connection.execute(
        "SELECT media AS path,id AS job_id,state,output,error,stage,"
        "json_extract(report,'$.audited_subtitle') AS audited_subtitle,"
        "json_type(report,'$.candidate')='text' AS has_candidate,"
        "json_extract(report,'$.selected_source_kind') AS selected_source_kind FROM jobs "
        "WHERE media NOT IN (SELECT path FROM managed_media)"
    ):
        record = dict(row)
        records.append(identity({**record, "provider": "folders"}))
    probes = {r["path"]: dict(r) for r in connection.execute("SELECT * FROM audio_metadata")}
    for record in records:
        probe = probes.get(record["path"])
        if probe:
            record["audio_languages"] = json.loads(probe["languages"])
            record["audio_source"] = "Audio track tags"
        elif record["audio_languages"]:
            record["audio_source"] = "Manager media info"
        else:
            record["audio_source"] = "Not inspected yet"
    return records


def preferences(connection) -> tuple[dict, bool]:
    rules = {(r["scope"], r["target"]): r["decision"] for r in connection.execute("SELECT * FROM library_rules")}
    setting = connection.execute("SELECT value FROM meta WHERE key='skip_other_audio'").fetchone()
    return rules, setting is None or setting[0] == "true"


def file_targets(record: dict) -> list[str]:
    if record.get("provider") == "sonarr" and record["season"] is not None and record["episodes"]:
        return [f"{record['key']}:episode:{record['season']}:{n}" for n in record["episodes"]]
    return [record["path"]]


def decision(record: dict, rules: dict, auto: bool) -> dict:
    # One physical file can contain several episodes. A skip on any member must
    # suppress that file; include applies when there is no explicit episode skip.
    targets = file_targets(record)
    choices = [rules.get(("file", target)) for target in targets]
    choice = "skip" if "skip" in choices else "include" if "include" in choices else None
    if choice:
        shared = " (shared episode file)" if len(targets) > 1 else ""
        return {"skipped": choice == "skip", "decision": choice, "decision_scope": "file",
                "reason": f"{'Skipped' if choice == 'skip' else 'Included'} by file preference{shared}"}
    scopes = []
    if record["season"] is not None:
        scopes.append(("season", f"{record['key']}:{record['season']}"))
    scopes.append(("title", record["key"]))
    for scope, target in scopes:
        choice = rules.get((scope, target))
        if choice:
            return {"skipped": choice == "skip", "decision": choice, "decision_scope": scope,
                    "reason": f"{'Skipped' if choice == 'skip' else 'Included'} by {scope} preference"}
    languages = record["audio_languages"]
    skipped = auto and bool(languages) and not {"en", "und"}.intersection(languages)
    return {"skipped": skipped, "decision": "auto", "decision_scope": "audio" if skipped else "default",
            "reason": "Audio tagged " + ", ".join(languages) + "; no English dialogue track" if skipped else
            "Audio language unknown; eligible" if not languages or "und" in languages else "Eligible for processing"}


def refresh_policy(connection) -> None:
    rules, auto = preferences(connection)
    for record in rows(connection):
        if not record.get("job_id"):
            continue
        result = decision(record, rules, auto)
        order = f"{record['title'].casefold()}|{record['key']}|{record['season'] or 0:04}|{min(record['episodes'] or [0]):05}|{record['path']}"
        blocked = result["reason"] if result["skipped"] else ""
        connection.execute("UPDATE jobs SET library_order=?,library_blocked=? WHERE id=? "
                           "AND (library_order<>? OR library_blocked<>?)",
                           (order, blocked, record["job_id"], order, blocked))
        if result["skipped"]:
            connection.execute(
                "UPDATE jobs SET state='skipped',stage='Library preference',error=? "
                "WHERE id=? AND state IN ('queued','waiting','retry')",
                (result["reason"], record["job_id"]),
            )
            connection.execute("UPDATE jobs SET cancel_requested=1 WHERE id=? AND state='processing'",
                               (record["job_id"],))
        else:
            connection.execute(
                "UPDATE jobs SET state=CASE WHEN ready>? THEN 'waiting' ELSE 'queued' END,"
                "stage='',error=NULL,cancel_requested=0,attempts=0 "
                "WHERE id=? AND state='skipped' AND stage='Library preference'", (time.time(), record["job_id"]),
            )


def library(connection, kind: str, query: str, offset: int, limit: int, status: str = "all") -> dict:
    rules, auto = preferences(connection)
    groups = {}
    for record in rows(connection):
        record.update(decision(record, rules, auto))
        downloadable = record.get("output") or (record.get("state") == "unchanged" and
                       (record.get("audited_subtitle") or record.get("selected_source_kind") in {"external", "embedded"}))
        record["download"] = f"/api/jobs/{record['job_id']}/subtitle" if downloadable else None
        record["candidate_download"] = (f"/api/jobs/{record['job_id']}/candidate"
                                        if record.get("state") == "review" and record.get("has_candidate") else None)
        record.pop("audited_subtitle", None)
        group = groups.setdefault(record["key"], {
            "key": record["key"], "title": record["title"], "kind": record["kind"],
            "provider": record["provider"], "item_id": record.get("item_id"), "year": record.get("year"),
            "poster": f"/api/library/artwork/{record['provider']}/{record['item_id']}" if record.get("poster") else None,
            "preference": rules.get(("title", record["key"]), "auto"), "files": [],
        })
        group["files"].append(record)
    counts = {kind: sum(g["kind"] == kind for g in groups.values()) for kind in ("movies", "shows")}
    selected = []
    for group in groups.values():
        group["files"].sort(key=lambda f: (f["season"] if f["season"] is not None else -1,
                                          min(f["episodes"] or [0]), f["path"]))
        group["file_count"] = len(group["files"])
        group["skipped_count"] = sum(f["skipped"] for f in group["files"])
        group["season_preferences"] = {str(f["season"]): rules.get(
            ("season", f"{group['key']}:{f['season']}"), "auto") for f in group["files"]}
        words = query.casefold().split()
        matches = any(all(w in (group["title"] + " " + f["path"] + " " +
                               f.get("episode_title", "")).casefold() for w in words) for f in group["files"])
        if group["kind"] != kind or not matches:
            continue
        if status == "skipped" and not group["skipped_count"]:
            continue
        if status == "eligible" and group["skipped_count"] == group["file_count"]:
            continue
        selected.append(group)
    selected.sort(key=lambda g: (bool(query) and query.casefold() not in g["title"].casefold(),
                                 g["title"].casefold(), g["key"]))
    return {"results": selected[offset:offset + limit], "total": len(selected), "offset": offset,
            "limit": limit, "counts": counts, "skip_other_audio": auto}
