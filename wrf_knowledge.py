"""Versioned, offline WRF evidence store. No model execution or business DB access."""
from __future__ import annotations

import argparse
import csv
from contextlib import closing
import hashlib
import io
import json
import math
import os
from pathlib import Path
import re
import sqlite3
import tempfile
import zipfile


DEFAULT_DB = Path(__file__).resolve().parents[1] / "knowledge" / "wrf" / "current.sqlite3"
# A member identifier is only unique within a paper.
TABLES = {
    "01_PAPER.csv": ("paper", ("paper_id",)),
    "02_EVENT.csv": ("event", ("paper_id", "event_id")),
    "03_MEMBER.csv": ("member", ("paper_id", "member_id")),
    "04_DOMAIN_PHYSICS.csv": ("domain_physics", ("domain_physics_id",)),
    "05_PARAMETER_OPTION.csv": ("parameter_option", ("parameter_record_id",)),
    "06_PERFORMANCE.csv": ("performance", ("performance_id",)),
    "07_EVIDENCE.csv": ("evidence", ("evidence_id",)),
    "08_SCHEME_DICTIONARY.csv": ("scheme_dictionary", ("scheme_id",)),
    "09_MEMBER_EVENT.csv": ("member_event", ("member_event_id",)),
    "10_OBSERVATION_SOURCE.csv": ("observation_source", ("observation_source_id",)),
    "11_METRIC_DICTIONARY.csv": ("metric_dictionary", ("metric_name_standard",)),
}
PHYSICS = {
    "MP": "mp_physics", "CU": "cu_physics", "PBL": "bl_pbl_physics",
    "SFCLAY": "sf_sfclay_physics", "LSM": "sf_surface_physics",
    "SW": "ra_sw_physics", "LW": "ra_lw_physics",
}
TARGETS = {"precipitation", "wind", "thermodynamics_pbl", "solar_radiation",
           "tropical_cyclone", "snow_ice", "urban_climate", "land_surface", "hydrology"}
ALIASES = {"中国": "China", "鄱阳湖": "Poyang", "青藏高原": "Tibetan Plateau",
           "北京": "Beijing", "首尔": "Seoul", "印度": "India", "伊朗": "Iran"}


def db_path() -> Path:
    return Path(os.getenv("WRF_KB_PATH", str(DEFAULT_DB))).expanduser().resolve()


def _json(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, allow_nan=False)


def _number(value):
    try:
        result = float(value)
        return result if math.isfinite(result) else None
    except (ValueError, TypeError):
        return None


def import_bundle(bundle: Path, destination: Path) -> dict:
    """Build a new snapshot atomically. Keep original CSV bytes and never infer values."""
    bundle, destination = Path(bundle), Path(destination)
    if destination.exists():
        raise ValueError("目标知识库已存在；请使用新的版本文件名，核验后再切换 WRF_KB_PATH")
    digest = hashlib.sha256(bundle.read_bytes()).hexdigest()
    report = {"counts": {}, "issues": [], "source_sha256": digest,
              "verification_status": "unverified_import", "schema_version": 1}
    rows_by_table = {}
    sources = {}
    with zipfile.ZipFile(bundle) as archive:
        names = archive.namelist()
        for name in ["manifest.json", *TABLES]:
            if names.count(name) != 1:
                raise ValueError(f"必须恰好包含一个 {name}")
            info = archive.getinfo(name)
            if info.file_size > 32 * 1024 * 1024:
                raise ValueError(f"文件超过导入限制：{name}")
            sources[name] = archive.read(name)
        manifest = json.loads(sources["manifest.json"])
        if not isinstance(manifest.get("version"), str) or not manifest["version"]:
            raise ValueError("manifest 缺少版本")
        report["version"] = manifest["version"]
        for filename, (table, keys) in TABLES.items():
            reader = csv.DictReader(io.StringIO(sources[filename].decode("utf-8-sig")))
            if not set(keys) <= set(reader.fieldnames or []):
                raise ValueError(f"{filename} 缺少主键字段")
            entries, seen = [], set()
            for source_row, row in enumerate(reader, 2):
                if None in row or any(value is None for value in row.values()):
                    raise ValueError(f"{filename}:{source_row} 列数不一致")
                key = _json([row[k] for k in keys])
                if any(not row[k].strip() for k in keys) or key in seen:
                    raise ValueError(f"{filename}:{source_row} 主键为空或重复：{key}")
                seen.add(key)
                entries.append((key, source_row, row))
            rows_by_table[table] = entries
            report["counts"][table] = len(entries)
    papers = {row["paper_id"] for _, _, row in rows_by_table["paper"]}
    members = {(row["paper_id"], row["member_id"]) for _, _, row in rows_by_table["member"]}
    events = {(row["paper_id"], row["event_id"]) for _, _, row in rows_by_table["event"]}
    for table, entries in rows_by_table.items():
        for key, source_row, row in entries:
            issues = []
            if row.get("paper_id") and row["paper_id"] not in papers:
                issues.append("unresolved_paper_reference")
            if row.get("member_id") and (row.get("paper_id"), row["member_id"]) not in members:
                issues.append("unresolved_member_reference")
            if row.get("event_id") and (row.get("paper_id"), row["event_id"]) not in events:
                issues.append("unresolved_event_reference")
            if table == "performance":
                if _number(row.get("metric_value")) is None:
                    issues.append("missing_or_invalid_metric_value")
                for field in ("source_locator", "lead_time_raw", "observation_source_id"):
                    if not row.get(field):
                        issues.append("missing_" + field)
            if table == "domain_physics":
                for prefix in PHYSICS:
                    if row.get(prefix + "_option_status") != "current_official":
                        issues.append("unverified_mapping_" + prefix)
            if issues:
                report["issues"].append({"table": table, "key": key, "source_row": source_row, "codes": issues})
    report["performance_papers"] = len({r["paper_id"] for _, _, r in rows_by_table["performance"]})
    for field, table in (("papers", "paper"), ("members", "member"), ("performance_rows", "performance")):
        if field in manifest and manifest[field] != report["counts"][table]:
            raise ValueError(f"manifest 计数不一致：{field}")
    destination.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=".wrf-kb-", suffix=".sqlite3", dir=destination.parent)
    os.close(fd)
    try:
        with closing(sqlite3.connect(temporary)) as connection:
            connection.executescript("""
                CREATE TABLE metadata (key TEXT PRIMARY KEY, value TEXT NOT NULL);
                CREATE TABLE sources (name TEXT PRIMARY KEY, sha256 TEXT NOT NULL, content BLOB NOT NULL);
                CREATE TABLE records (
                    kind TEXT NOT NULL, record_key TEXT NOT NULL, paper_id TEXT, member_id TEXT,
                    source_file TEXT NOT NULL, source_row INTEGER NOT NULL, payload TEXT NOT NULL,
                    eligible INTEGER NOT NULL DEFAULT 1,
                    verification_status TEXT NOT NULL DEFAULT 'unverified_import',
                    PRIMARY KEY(kind, record_key));
                CREATE INDEX member_lookup ON records(kind, paper_id, member_id);
                CREATE VIRTUAL TABLE paper_search USING fts5(paper_id UNINDEXED, text);
            """)
            connection.executemany("INSERT INTO metadata VALUES (?,?)", [("manifest", _json(manifest)), ("report", _json(report))])
            for filename, data in sources.items():
                connection.execute("INSERT INTO sources VALUES (?,?,?)", (filename, hashlib.sha256(data).hexdigest(), data))
            quarantined = {(item["table"], item["key"]) for item in report["issues"]
                           if any(code.startswith("unresolved_") and code.endswith("_reference") for code in item["codes"])}
            for filename, (table, _) in TABLES.items():
                for key, source_row, row in rows_by_table[table]:
                    connection.execute(
                        "INSERT INTO records(kind,record_key,paper_id,member_id,source_file,source_row,payload,eligible) VALUES (?,?,?,?,?,?,?,?)",
                        (table, key, row.get("paper_id"), row.get("member_id"), filename, source_row, _json(row), int((table, key) not in quarantined)))
                    if table == "paper":
                        connection.execute("INSERT INTO paper_search VALUES (?,?)", (row["paper_id"], " ".join(row.values())))
            connection.commit()
        # Hard-link publication fails rather than overwriting a concurrently created snapshot.
        os.link(temporary, destination)
    finally:
        Path(temporary).unlink(missing_ok=True)
    return report


class KnowledgeStore:
    def __init__(self, path: Path | None = None):
        self.path = Path(path) if path is not None else db_path()

    def _connect(self):
        if not self.path.is_file():
            raise FileNotFoundError("WRF 知识库尚未导入，请先执行离线导入")
        connection = sqlite3.connect(self.path.resolve().as_uri() + "?mode=ro", uri=True)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA query_only=ON")
        return connection

    def status(self) -> dict:
        with closing(self._connect()) as connection:
            report = json.loads(connection.execute("SELECT value FROM metadata WHERE key='report'").fetchone()[0])
        return {**{k: v for k, v in report.items() if k != "issues"}, "issue_count": len(report["issues"])}

    @staticmethod
    def _records(connection, kind, paper_id, member_id=None):
        sql = "SELECT * FROM records WHERE eligible=1 AND kind=? AND paper_id=?"
        params = [kind, paper_id]
        if member_id is not None:
            sql += " AND member_id=?"
            params.append(member_id)
        return [dict(json.loads(row["payload"]), provenance={
            "file": row["source_file"], "row": row["source_row"],
            "verification_status": row["verification_status"],
        }) for row in connection.execute(sql, params)]

    def recommend(self, *, target: str, region: str, dx_km: float,
                  season: str = "", query: str = "", limit: int = 3) -> dict:
        if target not in TARGETS:
            raise ValueError("不支持的目标变量组")
        if not region.strip() or _number(dx_km) is None or float(dx_km) <= 0:
            raise ValueError("必须提供区域与大于零的目标网格距 dx_km")
        if len(region) > 200 or len(query) > 500 or len(season) > 100:
            raise ValueError("检索条件过长")
        region = ALIASES.get(region.strip(), region.strip()).casefold()
        candidates = []
        with closing(self._connect()) as connection:
            report = json.loads(connection.execute("SELECT value FROM metadata WHERE key='report'").fetchone()[0])
            matched_ids = None
            tokens = re.findall(r"[\w]+", query)[:20]
            if tokens:
                matched_ids = {row[0] for row in connection.execute(
                    "SELECT paper_id FROM paper_search WHERE paper_search MATCH ?",
                    (" OR ".join('"' + token + '"' for token in tokens),))}
            for raw in connection.execute("SELECT payload FROM records WHERE kind='paper' ORDER BY paper_id"):
                paper = json.loads(raw[0])
                paper_id = paper["paper_id"]
                if target not in paper.get("target_group", "").split(";"):
                    continue
                if region not in paper.get("country_region_raw", "").casefold():
                    continue
                if season and season not in paper.get("season_class", "").split(";"):
                    continue
                if matched_ids is not None and paper_id not in matched_ids:
                    continue
                members = {r["member_id"]: r for r in self._records(connection, "member", paper_id)}
                for physics in self._records(connection, "domain_physics", paper_id):
                    dx = _number(physics.get("dx_km"))
                    member_id = physics["member_id"]
                    if member_id not in members or dx is None or abs(dx - float(dx_km)) > float(dx_km) * 0.25:
                        continue
                    proposal, unresolved = {}, []
                    for prefix, field in PHYSICS.items():
                        value = _number(physics.get(prefix + "_wrf_option"))
                        if physics.get(prefix + "_option_status") == "current_official" and value is not None and value.is_integer() and value >= 0:
                            proposal[field] = int(value)
                        else:
                            unresolved.append(field)
                    performance = self._records(connection, "performance", paper_id, member_id)
                    # Do not attribute aggregate/other-domain metrics to this domain.
                    domain_metrics = [r for r in performance if r.get("domain_id") == physics.get("domain_id")]
                    evidence = self._records(connection, "evidence", paper_id, member_id)
                    candidates.append({
                        "paper_id": paper_id, "member_id": member_id, "domain_id": physics["domain_id"],
                        "title": paper.get("paper_title"), "source_url": paper.get("source_url"),
                        "paper_wrf_version": paper.get("wrf_version_raw"),
                        "matched": {"region": paper.get("country_region_raw"), "target": target, "dx_km": dx,
                                    "season": paper.get("season_class")},
                        "author_best_reported": members[member_id].get("author_best_bool", "").lower() in {"true", "1", "yes"},
                        "physics_proposal": proposal, "unresolved_parameters": unresolved,
                        "domain_assignment_basis": physics.get("physics_assignment_note"),
                        "cu_assignment_basis": physics.get("CU_assignment_basis"),
                        "provenance": physics["provenance"], "evidence": evidence,
                        "performance": domain_metrics, "other_scope_performance_count": len(performance) - len(domain_metrics),
                        "runnable": False, "requires_runtime_validation": True,
                        "warnings": ["文献内容尚未经独立原文核验", "候选是单域文献配置，不是完整 WrfTaskCreate",
                                     "当前 WRF 版本、配套物理方案与运行约束尚未校验"],
                    })
        # Relevance only, never compare raw metrics from different experiments.
        candidates.sort(key=lambda c: (abs(c["matched"]["dx_km"] - float(dx_km)),
                                       not c["author_best_reported"], not bool(c["performance"]),
                                       c["paper_id"], c["member_id"], c["domain_id"]))
        selected, papers_seen = [], set()
        for candidate in candidates:
            if candidate["paper_id"] not in papers_seen:
                selected.append(candidate)
                papers_seen.add(candidate["paper_id"])
            if len(selected) >= max(1, min(int(limit), 3)):
                break
        return {"status": "ok" if selected else "no_evidence", "kb_version": report["version"],
                "source_sha256": report["source_sha256"], "candidates": selected,
                "matched_candidate_count": len(candidates), "ranking_basis": "grid_similarity_then_author_report_not_cross_paper_skill",
                "warnings": ["不保证全局最佳；不跨论文直接比较指标", "缺失参数保持缺失；不得用该响应直接提交任务"]}


def main():
    parser = argparse.ArgumentParser(description="离线导入 WRF CSV 知识库（不访问网络）")
    parser.add_argument("bundle", type=Path)
    parser.add_argument("--output", type=Path, default=DEFAULT_DB)
    args = parser.parse_args()
    report = import_bundle(args.bundle, args.output)
    print(_json({**{k: v for k, v in report.items() if k != "issues"}, "issue_count": len(report["issues"])}))


if __name__ == "__main__":
    main()
