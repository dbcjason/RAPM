#!/usr/bin/env python3
"""Pull lineups, play-by-play, and player shooting for teams listed in a CSV.

Design goal: minimal API usage.
- One request per team per endpoint per season type.
- No fallback request permutations.
"""

from __future__ import annotations

import argparse
import csv
import difflib
import hashlib
import io
import json
import os
import re
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

import pandas as pd


BASE_URL = "https://api.collegebasketballdata.com"
API_KEY = "SXMeiEWTsy0KNablQhQVQBL7LhVcVnubACJUUAoeT/xWHKo+kV0fAxjAjaHEc6Ph"


def log(msg: str) -> None:
    print(msg, flush=True)


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def season_label(year: int) -> str:
    return f"{year-1}-{year}"


def ensure_dir(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True)


def norm(s: Any) -> str:
    if s is None:
        return ""
    t = str(s).strip().lower()
    t = re.sub(r"[^a-z0-9]+", "", t)
    return t


def alias_variants(name: str) -> list[str]:
    out = {name.strip(), name.strip().replace(".", "")}
    s = name.strip()
    rules = [
        (" St.", " State"),
        ("Cal St.", "Cal State"),
        ("FIU", "Florida International"),
        ("LIU", "LIU Brooklyn"),
        ("Albany", "UAlbany"),
        ("American", "American University"),
        ("Appalachian St.", "App State"),
        ("Illinois Chicago", "UIC"),
        ("IU Indy", "IU Indianapolis"),
        ("Loyola MD", "Loyola Maryland"),
        ("Miami FL", "Miami"),
        ("Mississippi", "Ole Miss"),
        ("Nebraska Omaha", "Omaha"),
        ("Penn", "Pennsylvania"),
        ("Saint Francis", "St. Francis (PA)"),
        ("Southeastern Louisiana", "SE Louisiana"),
        ("Tennessee Martin", "UT Martin"),
        ("USC Upstate", "South Carolina Upstate"),
        ("Seattle", "Seattle U"),
        ("Queens", "Queens University"),
        ("San Jose St.", "San José State"),
        ("Louisiana Monroe", "UL Monroe"),
        ("Sam Houston St.", "Sam Houston"),
        ("Nicholls St.", "Nicholls"),
        ("McNeese St.", "McNeese"),
        ("Grambling St.", "Grambling"),
        ("Texas A&M Corpus Chris", "Texas A&M-Corpus Christi"),
        ("Long Beach St.", "Long Beach State"),
        ("Cal Baptist", "California Baptist"),
        ("Connecticut", "UConn"),
        ("UMKC", "Kansas City"),
    ]
    for a, b in rules:
        if a in s:
            out.add(s.replace(a, b))
    if s.startswith("Saint "):
        out.add(s.replace("Saint ", "St. "))
    if s.startswith("St. "):
        out.add(s.replace("St. ", "Saint "))
    return list(out)


def flatten_obj(obj: Any, prefix: str = "") -> dict[str, Any]:
    out: dict[str, Any] = {}
    if isinstance(obj, dict):
        for k, v in obj.items():
            key = f"{prefix}.{k}" if prefix else str(k)
            out.update(flatten_obj(v, key))
        return out
    if isinstance(obj, list):
        if not obj:
            out[prefix] = "[]"
            return out
        if all(not isinstance(x, (dict, list)) for x in obj):
            out[prefix] = json.dumps(obj, ensure_ascii=True)
            return out
        for i, v in enumerate(obj):
            key = f"{prefix}[{i}]"
            out.update(flatten_obj(v, key))
        return out
    out[prefix] = obj
    return out


def to_records(payload: Any) -> list[dict[str, Any]]:
    if isinstance(payload, list):
        return [x for x in payload if isinstance(x, dict)]
    if isinstance(payload, dict):
        for k in ("data", "results", "items", "rows"):
            v = payload.get(k)
            if isinstance(v, list):
                return [x for x in v if isinstance(x, dict)]
        return [payload]
    return []


def _csv_part_path(path: Path, part_idx: int) -> Path:
    if part_idx <= 1:
        return path
    return path.with_name(f"{path.stem}_part{part_idx:03d}{path.suffix}")


def _clear_existing_csv_parts(path: Path) -> None:
    for p in [path, *sorted(path.parent.glob(f"{path.stem}_part*{path.suffix}"))]:
        if p.exists():
            p.unlink()


def write_csv(rows: list[dict[str, Any]], path: Path, max_bytes: int = 0) -> None:
    ensure_dir(path.parent)
    _clear_existing_csv_parts(path)
    if not rows:
        path.write_text("", encoding="utf-8")
        return
    keys = sorted({k for r in rows for k in r.keys()})
    if max_bytes <= 0:
        with path.open("w", newline="", encoding="utf-8") as f:
            w = csv.DictWriter(f, fieldnames=keys)
            w.writeheader()
            for row in rows:
                w.writerow({k: row.get(k) for k in keys})
        return

    header_buf = io.StringIO()
    header_writer = csv.DictWriter(header_buf, fieldnames=keys)
    header_writer.writeheader()
    header_text = header_buf.getvalue()
    header_bytes = len(header_text.encode("utf-8"))

    row_buf = io.StringIO()
    row_writer = csv.DictWriter(row_buf, fieldnames=keys)

    part_idx = 1
    rows_in_part = 0
    bytes_in_part = 0
    part_path = _csv_part_path(path, part_idx)
    f = part_path.open("w", newline="", encoding="utf-8")
    f.write(header_text)
    bytes_in_part = header_bytes
    try:
        for row in rows:
            row_buf.seek(0)
            row_buf.truncate(0)
            row_writer.writerow({k: row.get(k) for k in keys})
            row_text = row_buf.getvalue()
            row_bytes = len(row_text.encode("utf-8"))
            if rows_in_part > 0 and (bytes_in_part + row_bytes) > max_bytes:
                f.close()
                part_idx += 1
                rows_in_part = 0
                part_path = _csv_part_path(path, part_idx)
                f = part_path.open("w", newline="", encoding="utf-8")
                f.write(header_text)
                bytes_in_part = header_bytes
            f.write(row_text)
            rows_in_part += 1
            bytes_in_part += row_bytes
    finally:
        f.close()


def merge_csv_files(inputs: list[Path], output: Path, max_bytes: int = 0) -> int:
    frames = []
    for p in inputs:
        if not p.exists() or p.stat().st_size == 0:
            continue
        try:
            frames.append(pd.read_csv(p, low_memory=False))
        except Exception:
            continue
    if not frames:
        output.write_text("", encoding="utf-8")
        return 0
    merged = pd.concat(frames, ignore_index=True)
    write_csv(merged.to_dict(orient="records"), output, max_bytes=max_bytes)
    return int(len(merged))


def is_numeric_series(s: pd.Series) -> bool:
    coerced = pd.to_numeric(s, errors="coerce")
    return coerced.notna().any()


def aggregate_player_shooting_fullseason(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    if not rows:
        return []
    df = pd.DataFrame(rows).copy()
    if df.empty:
        return []

    id_priority = [
        "playerId",
        "athleteId",
        "id",
        "player",
        "name",
        "__team_id",
        "__team_name",
        "teamId",
        "team",
        "season",
        "seasonLabel",
    ]
    id_cols = [c for c in id_priority if c in df.columns]
    if not id_cols:
        id_cols = [c for c in df.columns if c.startswith(("player", "name", "team", "season"))]
    if "__season_type" in id_cols:
        id_cols.remove("__season_type")

    numeric_cols: list[str] = []
    for c in df.columns:
        if c in id_cols or c == "__season_type":
            continue
        if is_numeric_series(df[c]):
            df[c] = pd.to_numeric(df[c], errors="coerce")
            numeric_cols.append(c)

    rate_cols = [c for c in numeric_cols if ("pct" in c.lower() or "percentage" in c.lower() or "rate" in c.lower())]
    sum_cols = [c for c in numeric_cols if c not in rate_cols]

    grouped = df.groupby(id_cols, dropna=False, as_index=False)
    out = grouped[sum_cols].sum(min_count=1) if sum_cols else grouped.size().drop(columns=["size"])

    for rc in rate_cols:
        attempt_candidates = [
            rc.replace("pct", "attempted").replace("Pct", "attempted"),
            rc.replace("percentage", "attempted").replace("Percentage", "attempted"),
            rc.replace("rate", "attempted").replace("Rate", "attempted"),
            rc.replace("pct", "att").replace("Pct", "att"),
        ]
        attempt_col = next((c for c in attempt_candidates if c in df.columns), None)
        vals = []
        for _, g in grouped:
            v = pd.to_numeric(g[rc], errors="coerce")
            if attempt_col is not None and attempt_col in g.columns:
                w = pd.to_numeric(g[attempt_col], errors="coerce")
                m = v.notna() & w.notna() & (w > 0)
                if m.any():
                    vals.append(float((v[m] * w[m]).sum() / w[m].sum()))
                else:
                    vals.append(float(v.mean(skipna=True)) if v.notna().any() else None)
            else:
                vals.append(float(v.mean(skipna=True)) if v.notna().any() else None)
        out[rc] = vals

    return out.to_dict(orient="records")


class Client:
    def __init__(
        self,
        api_key: str,
        sleep_sec: float = 0.15,
        timeout_sec: int = 60,
        max_requests: int = 5000,
        cache_dir: Path | None = None,
        cache_mode: str = "readwrite",
    ) -> None:
        self.api_key = api_key
        self.sleep_sec = sleep_sec
        self.timeout_sec = timeout_sec
        self.max_requests = max_requests
        self.cache_dir = cache_dir
        self.cache_mode = cache_mode
        self.request_count = 0
        self.cache_hits = 0
        self.request_log: list[dict[str, Any]] = []
        if self.cache_dir is not None:
            ensure_dir(self.cache_dir)

    @staticmethod
    def _normalized_params(params: dict[str, Any]) -> dict[str, Any]:
        return {k: params[k] for k in sorted(params.keys()) if params[k] is not None and params[k] != ""}

    def _cache_path(self, path: str, params: dict[str, Any]) -> Path | None:
        if self.cache_dir is None:
            return None
        key_payload = {"path": path, "params": self._normalized_params(params)}
        key = hashlib.sha1(json.dumps(key_payload, sort_keys=True, separators=(",", ":")).encode("utf-8")).hexdigest()
        return self.cache_dir / f"{key}.json"

    def get(self, path: str, params: dict[str, Any]) -> tuple[int, Any]:
        cache_path = self._cache_path(path, params)
        if self.cache_mode in {"readwrite", "readonly"} and cache_path is not None and cache_path.exists():
            try:
                cached = json.loads(cache_path.read_text(encoding="utf-8"))
                status = int(cached.get("status", 0))
                body = cached.get("body")
                self.cache_hits += 1
                self.request_log.append(
                    {
                        "ts_utc": utc_now(),
                        "path": path,
                        "params": json.dumps(self._normalized_params(params), ensure_ascii=True),
                        "status": status,
                        "error": "",
                        "duration_sec": 0.0,
                        "from_cache": True,
                    }
                )
                return status, body
            except Exception:
                # Ignore bad cache files and continue to live request.
                pass

        if self.request_count >= self.max_requests:
            raise RuntimeError(f"Request budget exceeded (max_requests={self.max_requests}).")
        norm_params = self._normalized_params(params)
        query = urlencode(norm_params)
        url = f"{BASE_URL}{path}" + (f"?{query}" if query else "")
        req = Request(
            url,
            headers={
                "Authorization": f"Bearer {self.api_key}",
                "Accept": "application/json",
                "User-Agent": "cbbd-lineups-plays-player-shooting/1.0",
            },
            method="GET",
        )
        status = 0
        body: Any = None
        error = ""
        t0 = time.time()
        try:
            with urlopen(req, timeout=self.timeout_sec) as resp:
                status = int(resp.status)
                txt = resp.read().decode("utf-8", errors="replace")
                body = json.loads(txt) if txt else None
        except HTTPError as e:
            status = int(e.code)
            txt = e.read().decode("utf-8", errors="replace")
            try:
                body = json.loads(txt) if txt else None
            except Exception:
                body = {"error_text": txt[:5000]}
            error = f"http_{e.code}"
        except URLError as e:
            body = {"error": str(e)}
            error = "url_error"
        except Exception as e:  # noqa: BLE001
            body = {"error": str(e)}
            error = "exception"
        finally:
            self.request_count += 1
            self.request_log.append(
                {
                    "ts_utc": utc_now(),
                    "path": path,
                    "params": json.dumps(norm_params, ensure_ascii=True),
                    "status": status,
                    "error": error,
                    "duration_sec": round(time.time() - t0, 3),
                    "from_cache": False,
                }
            )
            if cache_path is not None and self.cache_mode in {"readwrite", "refresh"}:
                try:
                    cache_path.write_text(
                        json.dumps(
                            {
                                "status": status,
                                "body": body,
                                "cached_utc": utc_now(),
                                "path": path,
                                "params": norm_params,
                            },
                            ensure_ascii=True,
                        ),
                        encoding="utf-8",
                    )
                except Exception:
                    pass
            time.sleep(self.sleep_sec)
        return status, body


def save_raw(base: Path, dataset: str, label: str, payload: Any) -> None:
    d = base / "raw" / dataset
    ensure_dir(d)
    safe = re.sub(r"[^A-Za-z0-9_.-]+", "_", label)
    with (d / f"{safe}.json").open("w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=True)


def read_requested_teams(csv_path: Path, team_col: str) -> list[str]:
    with csv_path.open(newline="", encoding="utf-8") as f:
        r = csv.DictReader(f)
        if team_col not in (r.fieldnames or []):
            raise RuntimeError(f"Column '{team_col}' not found in {csv_path}.")
        teams = sorted({row.get(team_col, "").strip() for row in r if row.get(team_col, "").strip()})
    return teams


def discover_teams(client: Client, year: int) -> list[dict[str, Any]]:
    # Keep this cheap: one call.
    status, payload = client.get("/teams", {"season": year})
    if status != 200:
        return []
    recs = to_records(payload)
    out = []
    for r in recs:
        out.append(
            {
                "team_id": r.get("id"),
                "team_name": r.get("school") or r.get("team") or r.get("name"),
                "conference": r.get("conference"),
            }
        )
    return out


def map_teams(requested_names: list[str], discovered: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], list[str]]:
    idx: dict[str, dict[str, Any]] = {}
    for t in discovered:
        key = norm(t.get("team_name"))
        if key and key not in idx:
            idx[key] = t
    norm_keys = list(idx.keys())
    matched: list[dict[str, Any]] = []
    unmatched: list[str] = []
    for name in requested_names:
        team = None
        for variant in alias_variants(name):
            team = idx.get(norm(variant))
            if team:
                break
        if team is None:
            cands = difflib.get_close_matches(norm(name), norm_keys, n=1, cutoff=0.86)
            if cands:
                team = idx[cands[0]]
        if team is None:
            unmatched.append(name)
        else:
            matched.append(team)
    # dedupe
    out: list[dict[str, Any]] = []
    seen = set()
    for t in matched:
        k = f"id:{t.get('team_id')}" if t.get("team_id") is not None else f"name:{t.get('team_name')}"
        if k in seen:
            continue
        seen.add(k)
        out.append(t)
    return out, unmatched


def pull_team_endpoint(
    client: Client,
    base: Path,
    dataset: str,
    endpoint: str,
    teams: list[dict[str, Any]],
    year: int,
    season_type: str,
) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    n = len(teams)
    for i, team in enumerate(teams, start=1):
        team_id = team.get("team_id")
        team_name = team.get("team_name")
        # One request only to minimize pulls.
        params = {"season": year, "seasonType": season_type, "team": team_id if team_id is not None else team_name}
        status, payload = client.get(endpoint, params)
        save_raw(base, dataset, f"{season_type}_{team_name or team_id}_{status}", payload)
        if status == 200:
            recs = to_records(payload)
            for r in recs:
                r["__team_id"] = team_id
                r["__team_name"] = team_name
                r["__season_type"] = season_type
            rows.extend(recs)
        if i == 1 or i % 25 == 0 or i == n:
            log(f"[{dataset}] {season_type}: team {i}/{n} requests={client.request_count} rows={len(rows)}")
    return rows


def get_games_for_team(
    client: Client,
    base: Path,
    team_name: str,
    year: int,
    season_type: str,
) -> list[dict[str, Any]]:
    params = {"season": year, "team": team_name, "seasonType": season_type}
    status, payload = client.get("/games", params)
    save_raw(base, f"games_{season_type}", f"{team_name}_{status}", payload)
    return to_records(payload) if status == 200 else []


def date_range_from_games(games: list[dict[str, Any]]) -> tuple[str | None, str | None]:
    dates: list[str] = []
    for g in games:
        dt = g.get("startDate")
        if isinstance(dt, str) and dt:
            dates.append(dt[:10])
    if not dates:
        return None, None
    return min(dates), max(dates)


def get_lineups_for_team_range(
    client: Client,
    base: Path,
    team_name: str,
    year: int,
    season_type: str,
    start_date: str | None,
    end_date: str | None,
) -> list[dict[str, Any]]:
    params = {"season": year, "team": team_name}
    if start_date:
        params["startDateRange"] = start_date
    if end_date:
        params["endDateRange"] = end_date
    status, payload = client.get("/lineups/team", params)
    save_raw(base, f"lineups_{season_type}", f"{team_name}_{status}", payload)
    recs = to_records(payload) if status == 200 else []
    for r in recs:
        r["__season_type"] = season_type
    return recs


def get_plays_for_team_fullseason(client: Client, base: Path, team_name: str, year: int) -> list[dict[str, Any]]:
    params = {"season": year, "team": team_name}
    status, payload = client.get("/plays/team", params)
    save_raw(base, "plays_fullseason_raw", f"{team_name}_{status}", payload)
    return to_records(payload) if status == 200 else []


def split_plays_by_game_ids(
    plays: list[dict[str, Any]],
    regular_game_ids: set[int],
    postseason_game_ids: set[int],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], list[dict[str, Any]]]:
    reg: list[dict[str, Any]] = []
    post: list[dict[str, Any]] = []
    unknown: list[dict[str, Any]] = []
    for p in plays:
        gid = p.get("gameId")
        try:
            gid_int = int(gid) if gid is not None else None
        except Exception:
            gid_int = None
        row = dict(p)
        if gid_int is not None and gid_int in regular_game_ids:
            row["__season_type"] = "regular"
            reg.append(row)
        elif gid_int is not None and gid_int in postseason_game_ids:
            row["__season_type"] = "postseason"
            post.append(row)
        else:
            row["__season_type"] = "unknown"
            unknown.append(row)
    return reg, post, unknown


def filter_player_shooting_to_matched(
    rows: list[dict[str, Any]],
    matched: list[dict[str, Any]],
    season_type: str,
) -> list[dict[str, Any]]:
    team_ids = {t.get("team_id") for t in matched if t.get("team_id") is not None}
    team_names = {str(t.get("team_name")).strip().lower() for t in matched if t.get("team_name")}

    out: list[dict[str, Any]] = []
    for r in rows:
        rid = r.get("teamId")
        rname = (
            r.get("team")
            or r.get("teamName")
            or r.get("team_name")
            or r.get("school")
            or r.get("__team_name")
        )
        keep = False
        if rid is not None:
            try:
                keep = int(rid) in {int(x) for x in team_ids}
            except Exception:
                keep = rid in team_ids
        if not keep and rname is not None:
            keep = str(rname).strip().lower() in team_names
        if keep:
            row = dict(r)
            row["__season_type"] = season_type
            out.append(row)
    return out


def main() -> None:
    parser = argparse.ArgumentParser(description="Pull lineups + plays + player shooting for teams in a CSV.")
    parser.add_argument("--year", type=int, default=2025, help="Season year (2025 => 2024-2025).")
    parser.add_argument(
        "--teams-csv",
        default="2025_team_results (1).csv",
        help="CSV with team list.",
    )
    parser.add_argument("--team-col", default="team", help="Team column in the CSV.")
    parser.add_argument("--season-type", default="both", choices=["regular", "postseason", "both"])
    parser.add_argument(
        "--datasets",
        default="both",
        choices=["lineups", "plays", "both"],
        help="Choose what to pull: lineups only, plays only, or both.",
    )
    parser.add_argument(
        "--include-player-shooting",
        action="store_true",
        help="Also pull player shooting tables (off by default).",
    )
    parser.add_argument("--sleep-sec", type=float, default=0.15)
    parser.add_argument("--max-requests", type=int, default=3000)
    parser.add_argument(
        "--max-csv-mb",
        type=float,
        default=95.0,
        help="Max size per CSV file part in MB (0 disables splitting).",
    )
    parser.add_argument("--team-start", type=int, default=1, help="1-based start index within matched teams.")
    parser.add_argument("--team-end", type=int, default=0, help="1-based end index within matched teams. 0 means all.")
    parser.add_argument(
        "--chunk-tag",
        default="",
        help="Optional suffix tag for chunk outputs, e.g. chunk001_100.",
    )
    parser.add_argument(
        "--merge-chunks",
        action="store_true",
        help="After this run, merge all chunk CSV files into single full files.",
    )
    parser.add_argument(
        "--merge-only",
        action="store_true",
        help="Skip API pulling and only merge existing chunk files.",
    )
    parser.add_argument(
        "--cache-mode",
        default="readwrite",
        choices=["none", "readwrite", "readonly", "refresh"],
        help="HTTP cache mode for API responses.",
    )
    parser.add_argument(
        "--cache-dir",
        default="",
        help="Optional cache directory. Defaults to <out>/<season>/.http_cache",
    )
    parser.add_argument(
        "--out-root",
        default="cbbd_seasons",
        help="Output root folder.",
    )
    args = parser.parse_args()
    max_csv_bytes = int(max(args.max_csv_mb, 0) * 1024 * 1024)

    # Support GitHub Actions and local runs without editing code.
    api_key = os.environ.get("CBBD_API_KEY", API_KEY).strip()
    if not api_key:
        raise RuntimeError("Missing API key. Set CBBD_API_KEY or set API_KEY in the script.")

    out = Path(args.out_root) / season_label(args.year)
    ensure_dir(out / "raw")
    ensure_dir(out / "tables")
    ensure_dir(out / "manifest")
    cache_dir = Path(args.cache_dir) if args.cache_dir else out / ".http_cache"
    use_lineups = args.datasets in {"lineups", "both"}
    use_plays = args.datasets in {"plays", "both"}
    chunk_suffix = f"_{args.chunk_tag.strip()}" if args.chunk_tag.strip() else ""

    def table_path(stem: str) -> Path:
        return out / "tables" / f"{stem}{chunk_suffix}.csv"

    def manifest_path(stem: str, ext: str = "csv") -> Path:
        return out / "manifest" / f"{stem}{chunk_suffix}.{ext}"

    teams_csv_path = Path(args.teams_csv)
    if not teams_csv_path.exists():
        raise RuntimeError(
            f"teams CSV not found: {teams_csv_path}. "
            "Use --teams-csv with a repository-relative path."
        )

    client = Client(
        api_key=api_key,
        sleep_sec=args.sleep_sec,
        max_requests=args.max_requests,
        cache_dir=cache_dir if args.cache_mode != "none" else None,
        cache_mode=args.cache_mode,
    )
    requested = read_requested_teams(teams_csv_path, args.team_col)
    log(f"[teams] requested={len(requested)}")

    discovered = discover_teams(client, args.year)
    log(f"[teams] discovered={len(discovered)}")
    matched, unmatched = map_teams(requested, discovered)
    log(f"[teams] matched={len(matched)} unmatched={len(unmatched)}")

    write_csv([flatten_obj(x) for x in matched], out / "tables" / "target_teams_matched.csv", max_bytes=max_csv_bytes)
    write_csv(
        [{"team_name_unmatched": x} for x in unmatched],
        out / "tables" / "target_teams_unmatched.csv",
        max_bytes=max_csv_bytes,
    )

    if args.merge_only:
        args.merge_chunks = True

    total_matched = len(matched)
    start_idx = max(1, int(args.team_start))
    end_idx = int(args.team_end) if int(args.team_end) > 0 else total_matched
    end_idx = min(end_idx, total_matched)
    if start_idx > end_idx and not args.merge_only:
        raise RuntimeError(f"Invalid team range: start={start_idx} end={end_idx} total_matched={total_matched}")
    if not args.merge_only:
        matched = matched[start_idx - 1 : end_idx]
        log(f"[teams] active_range={start_idx}-{end_idx} active_matched={len(matched)}")

    season_types = ["regular", "postseason"] if args.season_type == "both" else [args.season_type]
    summary: dict[str, Any] = {
        "year": args.year,
        "season_label": season_label(args.year),
        "requested_teams": len(requested),
        "matched_teams": len(matched),
        "unmatched_teams": len(unmatched),
        "matched_teams_total": total_matched,
        "team_start": start_idx,
        "team_end": end_idx,
        "chunk_tag": args.chunk_tag,
        "season_types": season_types,
        "datasets": args.datasets,
        "include_player_shooting": bool(args.include_player_shooting),
        "cache_mode": args.cache_mode,
        "cache_dir": str(cache_dir) if args.cache_mode != "none" else "",
        "started_utc": utc_now(),
        "dataset_rows": {},
    }

    if not args.merge_only:
        for st in season_types:
            lineups = []
            plays = []
            plays_unknown = []
            player_shooting: list[dict[str, Any]] = []
            if args.include_player_shooting:
                # Player shooting: global pull then local filter (team-scoped calls return [] for many teams).
                ps_params = {"season": args.year, "seasonType": st}
                ps_status, ps_payload = client.get("/stats/player/shooting/season", ps_params)
                save_raw(out, f"player_shooting_{st}", f"global_{ps_status}", ps_payload)
                ps_all = to_records(ps_payload) if ps_status == 200 else []
                player_shooting = filter_player_shooting_to_matched(ps_all, matched, st)
                log(
                    f"[player_shooting_{st}] global_rows={len(ps_all)} "
                    f"filtered_rows={len(player_shooting)} requests={client.request_count} cache_hits={client.cache_hits}"
                )
            n = len(matched)
            for i, team in enumerate(matched, start=1):
                team_name = str(team.get("team_name") or "")
                team_id = team.get("team_id")

                need_reg_games = (use_lineups and st == "regular") or (use_plays and st == "regular")
                need_post_games = (use_lineups and st == "postseason") or (use_plays and st == "postseason")

                reg_ids: set[int] = set()
                reg_start = None
                reg_end = None
                if need_reg_games:
                    games_reg = get_games_for_team(client, out, team_name, args.year, "regular")
                    reg_ids = {int(g["id"]) for g in games_reg if g.get("id") is not None}
                    reg_start, reg_end = date_range_from_games(games_reg)

                post_ids: set[int] = set()
                post_start = None
                post_end = None
                if need_post_games:
                    games_post = get_games_for_team(client, out, team_name, args.year, "postseason")
                    post_ids = {int(g["id"]) for g in games_post if g.get("id") is not None}
                    post_start, post_end = date_range_from_games(games_post)

                if st == "regular":
                    if use_lineups:
                        l_reg = get_lineups_for_team_range(client, out, team_name, args.year, "regular", reg_start, reg_end)
                        for r in l_reg:
                            r["__team_id"] = team_id
                            r["__team_name"] = team_name
                        lineups.extend(l_reg)

                    if use_plays:
                        p_full = get_plays_for_team_fullseason(client, out, team_name, args.year)
                        p_reg, _, p_unknown = split_plays_by_game_ids(p_full, reg_ids, set())
                        for r in p_reg:
                            r["__team_id"] = team_id
                            r["__team_name"] = team_name
                        for r in p_unknown:
                            r["__team_id"] = team_id
                            r["__team_name"] = team_name
                        plays.extend(p_reg)
                        plays_unknown.extend(p_unknown)

                elif st == "postseason":
                    if use_lineups:
                        l_post = get_lineups_for_team_range(
                            client, out, team_name, args.year, "postseason", post_start, post_end
                        )
                        for r in l_post:
                            r["__team_id"] = team_id
                            r["__team_name"] = team_name
                        lineups.extend(l_post)

                    if use_plays:
                        p_full = get_plays_for_team_fullseason(client, out, team_name, args.year)
                        _, p_post, p_unknown = split_plays_by_game_ids(p_full, set(), post_ids)
                        for r in p_post:
                            r["__team_id"] = team_id
                            r["__team_name"] = team_name
                        for r in p_unknown:
                            r["__team_id"] = team_id
                            r["__team_name"] = team_name
                        plays.extend(p_post)
                        plays_unknown.extend(p_unknown)

                if i == 1 or i % 25 == 0 or i == n:
                    log(
                        f"[lineups_plays_{st}] team {i}/{n} requests={client.request_count} "
                        f"cache_hits={client.cache_hits} lineups_rows={len(lineups)} plays_rows={len(plays)}"
                    )

            if use_lineups:
                write_csv([flatten_obj(r) for r in lineups], table_path(f"lineups_{st}"), max_bytes=max_csv_bytes)
                summary["dataset_rows"][f"lineups_{st}"] = {"rows": len(lineups)}
            if use_plays:
                write_csv([flatten_obj(r) for r in plays], table_path(f"plays_{st}"), max_bytes=max_csv_bytes)
                write_csv(
                    [flatten_obj(r) for r in plays_unknown],
                    table_path(f"plays_{st}_unknown_game_map"),
                    max_bytes=max_csv_bytes,
                )
                summary["dataset_rows"][f"plays_{st}"] = {"rows": len(plays)}
                summary["dataset_rows"][f"plays_{st}_unknown_game_map"] = {"rows": len(plays_unknown)}
            if args.include_player_shooting:
                write_csv(
                    [flatten_obj(r) for r in player_shooting],
                    table_path(f"player_shooting_{st}"),
                    max_bytes=max_csv_bytes,
                )
                summary["dataset_rows"][f"player_shooting_{st}"] = {"rows": len(player_shooting)}

    # If both season types were requested, also write full-season combined tables.
    if args.season_type == "both" and not chunk_suffix and not args.merge_only:
        full_lineups: list[dict[str, Any]] = []
        full_plays: list[dict[str, Any]] = []
        full_player_shooting: list[dict[str, Any]] = []
        for st in ("regular", "postseason"):
            if use_lineups:
                p_lineups = table_path(f"lineups_{st}")
                if p_lineups.exists() and p_lineups.stat().st_size > 0:
                    full_lineups.extend(pd.read_csv(p_lineups, low_memory=False).to_dict(orient="records"))
            if use_plays:
                p_plays = table_path(f"plays_{st}")
                if p_plays.exists() and p_plays.stat().st_size > 0:
                    full_plays.extend(pd.read_csv(p_plays, low_memory=False).to_dict(orient="records"))
            if args.include_player_shooting:
                p_player_shooting = table_path(f"player_shooting_{st}")
                if p_player_shooting.exists() and p_player_shooting.stat().st_size > 0:
                    full_player_shooting.extend(pd.read_csv(p_player_shooting, low_memory=False).to_dict(orient="records"))
        if use_lineups:
            write_csv(full_lineups, table_path("lineups_fullseason"), max_bytes=max_csv_bytes)
            summary["dataset_rows"]["lineups_fullseason"] = {"rows": len(full_lineups)}
        if use_plays:
            write_csv(full_plays, table_path("plays_fullseason"), max_bytes=max_csv_bytes)
            summary["dataset_rows"]["plays_fullseason"] = {"rows": len(full_plays)}
        if args.include_player_shooting:
            write_csv(full_player_shooting, table_path("player_shooting_fullseason_raw"), max_bytes=max_csv_bytes)
            player_shooting_agg = aggregate_player_shooting_fullseason(full_player_shooting)
            write_csv(player_shooting_agg, table_path("player_shooting_fullseason"), max_bytes=max_csv_bytes)
            summary["dataset_rows"]["player_shooting_fullseason_raw"] = {"rows": len(full_player_shooting)}
            summary["dataset_rows"]["player_shooting_fullseason"] = {"rows": len(player_shooting_agg)}

    if args.merge_chunks:
        for st in season_types:
            if use_lineups:
                files = sorted((out / "tables").glob(f"lineups_{st}_*.csv"))
                if not chunk_suffix:
                    files.append(out / "tables" / f"lineups_{st}.csv")
                nrows = merge_csv_files(files, out / "tables" / f"lineups_{st}.csv", max_bytes=max_csv_bytes)
                summary["dataset_rows"][f"lineups_{st}_merged"] = {"rows": nrows, "chunks": len(files)}
            if use_plays:
                files = sorted((out / "tables").glob(f"plays_{st}_*.csv"))
                if not chunk_suffix:
                    files.append(out / "tables" / f"plays_{st}.csv")
                nrows = merge_csv_files(files, out / "tables" / f"plays_{st}.csv", max_bytes=max_csv_bytes)
                summary["dataset_rows"][f"plays_{st}_merged"] = {"rows": nrows, "chunks": len(files)}
                files = sorted((out / "tables").glob(f"plays_{st}_unknown_game_map_*.csv"))
                if not chunk_suffix:
                    files.append(out / "tables" / f"plays_{st}_unknown_game_map.csv")
                nrows = merge_csv_files(
                    files,
                    out / "tables" / f"plays_{st}_unknown_game_map.csv",
                    max_bytes=max_csv_bytes,
                )
                summary["dataset_rows"][f"plays_{st}_unknown_game_map_merged"] = {"rows": nrows, "chunks": len(files)}
            if args.include_player_shooting:
                files = sorted((out / "tables").glob(f"player_shooting_{st}_*.csv"))
                if not chunk_suffix:
                    files.append(out / "tables" / f"player_shooting_{st}.csv")
                nrows = merge_csv_files(
                    files,
                    out / "tables" / f"player_shooting_{st}.csv",
                    max_bytes=max_csv_bytes,
                )
                summary["dataset_rows"][f"player_shooting_{st}_merged"] = {"rows": nrows, "chunks": len(files)}

        if args.season_type == "both":
            if use_lineups:
                nrows = merge_csv_files(
                    [out / "tables" / "lineups_regular.csv", out / "tables" / "lineups_postseason.csv"],
                    out / "tables" / "lineups_fullseason.csv",
                    max_bytes=max_csv_bytes,
                )
                summary["dataset_rows"]["lineups_fullseason_merged"] = {"rows": nrows}
            if use_plays:
                nrows = merge_csv_files(
                    [out / "tables" / "plays_regular.csv", out / "tables" / "plays_postseason.csv"],
                    out / "tables" / "plays_fullseason.csv",
                    max_bytes=max_csv_bytes,
                )
                summary["dataset_rows"]["plays_fullseason_merged"] = {"rows": nrows}
            if args.include_player_shooting:
                nrows = merge_csv_files(
                    [out / "tables" / "player_shooting_regular.csv", out / "tables" / "player_shooting_postseason.csv"],
                    out / "tables" / "player_shooting_fullseason_raw.csv",
                    max_bytes=max_csv_bytes,
                )
                summary["dataset_rows"]["player_shooting_fullseason_raw_merged"] = {"rows": nrows}
                full_raw_path = out / "tables" / "player_shooting_fullseason_raw.csv"
                if full_raw_path.exists() and full_raw_path.stat().st_size > 0:
                    full_raw = pd.read_csv(full_raw_path, low_memory=False).to_dict(orient="records")
                    write_csv(
                        aggregate_player_shooting_fullseason(full_raw),
                        out / "tables" / "player_shooting_fullseason.csv",
                        max_bytes=max_csv_bytes,
                    )

    write_csv(client.request_log, manifest_path("requests_log"), max_bytes=max_csv_bytes)
    summary["request_count"] = client.request_count
    summary["cache_hits"] = client.cache_hits
    summary["finished_utc"] = utc_now()
    with manifest_path("run_summary", "json").open("w", encoding="utf-8") as f:
        json.dump(summary, f, ensure_ascii=True, indent=2)

    log(f"[run] saved={out}")
    log(f"[run] requests={client.request_count}")


if __name__ == "__main__":
    main()
