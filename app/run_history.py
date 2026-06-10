from __future__ import annotations

import csv
import io
import json
import logging
import sqlite3
import uuid
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

logger = logging.getLogger(__name__)

# Production launch of the verified pipeline: time-saved totals count runs
# from this date forward (Javier, 2026-06-10).
SAVINGS_START_DATE = "2026-06-10"
SAVINGS_START_LABEL = "Jun 10"


@dataclass
class RunLogRecord:
    source_filename: str
    status: str
    duration_ms: int
    output_filename: str = ""
    pages_processed: Optional[int] = None
    model: str = ""
    confidence: Optional[float] = None
    detected_totals_count: int = 0
    extra_billing_codes_count: int = 0
    selected_extras: list[dict[str, str]] | None = None
    warnings_count: int = 0
    error_type: str = ""
    error_message: str = ""
    estimated_minutes_saved: float = 0.0
    estimated_dollars_saved: float = 0.0
    input_pdf: bytes | None = None
    output_pdf: bytes | None = None
    result_lines: list[str] | None = None


class RunHistoryStore:
    def __init__(
        self,
        *,
        database_url: Optional[str],
        sqlite_path: str,
        savings_minutes_per_completed_pdf: float,
        savings_hourly_rate: float,
    ) -> None:
        self.database_url = database_url
        self.sqlite_path = sqlite_path
        self.savings_minutes_per_completed_pdf = savings_minutes_per_completed_pdf
        self.savings_hourly_rate = savings_hourly_rate
        self._initialized = False
        self._backend = "postgres" if database_url else "sqlite"

    @property
    def assumptions(self) -> dict[str, Any]:
        return {
            "minutes_per_completed_pdf": self.savings_minutes_per_completed_pdf,
            "hourly_rate": self.savings_hourly_rate,
            "label": "Minutes confirmed by Nick 2026-06-08 (5-10 min/as-built, itemized ~8); dollar rate pending",
        }

    def estimate_savings(self, status: str, has_output: bool) -> tuple[float, float]:
        if status not in {"success", "manual_review"} or not has_output:
            return 0.0, 0.0
        minutes = max(0.0, float(self.savings_minutes_per_completed_pdf))
        dollars = minutes / 60.0 * max(0.0, float(self.savings_hourly_rate))
        return round(minutes, 2), round(dollars, 2)

    def log_run(self, record: RunLogRecord) -> None:
        try:
            self._ensure_schema()
            row = self._record_to_row(record)
            if self._backend == "postgres":
                self._insert_postgres(row)
            else:
                self._insert_sqlite(row)
        except Exception as exc:  # noqa: BLE001 - logging must not break PDF processing
            logger.warning("run_history_log_failed error=%s", exc)

    def dashboard(self, limit: int = 20, query: str = "") -> dict[str, Any]:
        self._ensure_schema()
        bounded_limit = max(1, min(int(limit or 20), 500))
        needle = (query or "").strip().lower()
        if self._backend == "postgres":
            rows = self._fetch_recent_postgres(bounded_limit, needle)
            summary = self._summary_postgres()
        else:
            rows = self._fetch_recent_sqlite(bounded_limit, needle)
            summary = self._summary_sqlite()
        return {
            "summary": summary,
            "nick_review": self._nick_review(summary),
            "query": query or "",
            "runs": [self._public_row(row) for row in rows],
        }

    def get_pdf(self, run_id: str, kind: str) -> tuple[str, bytes] | None:
        """Return (download_filename, pdf_bytes) for a stored run PDF, or None."""
        if kind not in {"input", "output"}:
            return None
        self._ensure_schema()
        column = "input_pdf" if kind == "input" else "output_pdf"
        name_column = "source_filename" if kind == "input" else "output_filename"
        if self._backend == "postgres":
            import psycopg

            with psycopg.connect(self.database_url) as conn:
                row = conn.execute(
                    f"select {name_column}, {column} from asbuilt_run_history where id = %s",
                    (run_id,),
                ).fetchone()
        else:
            with sqlite3.connect(self.sqlite_path) as conn:
                row = conn.execute(
                    f"select {name_column}, {column} from asbuilt_run_history where id = ?",
                    (run_id,),
                ).fetchone()
        if not row or not row[1]:
            return None
        name = row[0] or f"run-{run_id}-{kind}.pdf"
        if not name.lower().endswith(".pdf"):
            name = f"{name}.pdf"
        return name, bytes(row[1])

    def csv_export(self, limit: int = 500) -> str:
        data = self.dashboard(limit=max(1, min(int(limit or 500), 2000)))
        output = io.StringIO()
        writer = csv.DictWriter(
            output,
            fieldnames=[
                "created_at",
                "status",
                "source_filename",
                "output_filename",
                "duration_seconds",
                "pages_processed",
                "model",
                "confidence",
                "detected_totals_count",
                "extra_billing_codes_count",
                "warnings_count",
                "error_type",
                "error_message",
                "estimated_minutes_saved",
            ],
        )
        writer.writeheader()
        for run in data["runs"]:
            writer.writerow({key: run.get(key, "") for key in writer.fieldnames})
        return output.getvalue()

    def _record_to_row(self, record: RunLogRecord) -> dict[str, Any]:
        data = asdict(record)
        data["id"] = uuid.uuid4().hex
        data["created_at"] = datetime.now(timezone.utc).isoformat()
        data["selected_extras_json"] = json.dumps(data.pop("selected_extras") or [], ensure_ascii=True)
        data["result_lines_json"] = json.dumps(data.pop("result_lines") or [], ensure_ascii=True)
        return data

    def _ensure_schema(self) -> None:
        if self._initialized:
            return
        if self._backend == "postgres":
            self._ensure_postgres_schema()
        else:
            self._ensure_sqlite_schema()
        self._initialized = True

    def _ensure_sqlite_schema(self) -> None:
        Path(self.sqlite_path).parent.mkdir(parents=True, exist_ok=True)
        with sqlite3.connect(self.sqlite_path) as conn:
            conn.executescript(_SQLITE_SCHEMA)
            existing = {row[1] for row in conn.execute("pragma table_info(asbuilt_run_history)")}
            for column, ddl in _SQLITE_MIGRATIONS:
                if column not in existing:
                    conn.execute(ddl)

    def _ensure_postgres_schema(self) -> None:
        import psycopg

        with psycopg.connect(self.database_url, autocommit=True) as conn:
            for statement in _POSTGRES_SCHEMA.split(";"):
                if statement.strip():
                    conn.execute(statement)
            # Idempotent column migrations: CREATE TABLE IF NOT EXISTS does not
            # add new columns to an existing table.
            for statement in _POSTGRES_MIGRATIONS:
                conn.execute(statement)

    def _insert_sqlite(self, row: dict[str, Any]) -> None:
        with sqlite3.connect(self.sqlite_path) as conn:
            conn.execute(_SQLITE_INSERT_SQL, row)

    def _insert_postgres(self, row: dict[str, Any]) -> None:
        import psycopg

        with psycopg.connect(self.database_url, autocommit=True) as conn:
            conn.execute(_POSTGRES_INSERT_SQL, row)

    def _fetch_recent_sqlite(self, limit: int, needle: str = "") -> list[dict[str, Any]]:
        with sqlite3.connect(self.sqlite_path) as conn:
            conn.row_factory = sqlite3.Row
            if needle:
                rows = conn.execute(
                    f"select {_LIST_COLUMNS} from asbuilt_run_history"
                    " where lower(source_filename) like ? or lower(output_filename) like ?"
                    "   or lower(status) like ? or id = ?"
                    " order by created_at desc limit ?",
                    (f"%{needle}%", f"%{needle}%", f"%{needle}%", needle, limit),
                ).fetchall()
            else:
                rows = conn.execute(
                    f"select {_LIST_COLUMNS} from asbuilt_run_history order by created_at desc limit ?",
                    (limit,),
                ).fetchall()
        return [dict(row) for row in rows]

    def _fetch_recent_postgres(self, limit: int, needle: str = "") -> list[dict[str, Any]]:
        import psycopg
        from psycopg.rows import dict_row

        with psycopg.connect(self.database_url, row_factory=dict_row) as conn:
            with conn.cursor() as cur:
                if needle:
                    cur.execute(
                        f"select {_LIST_COLUMNS} from asbuilt_run_history"
                        " where lower(source_filename) like %s or lower(output_filename) like %s"
                        "   or lower(status) like %s or id = %s"
                        " order by created_at desc limit %s",
                        (f"%{needle}%", f"%{needle}%", f"%{needle}%", needle, limit),
                    )
                else:
                    cur.execute(
                        f"select {_LIST_COLUMNS} from asbuilt_run_history order by created_at desc limit %s",
                        (limit,),
                    )
                return list(cur.fetchall())

    def _summary_sqlite(self) -> dict[str, Any]:
        with sqlite3.connect(self.sqlite_path) as conn:
            conn.row_factory = sqlite3.Row
            row = conn.execute(_SUMMARY_SQL).fetchone()
        return self._public_summary(dict(row or {}))

    def _summary_postgres(self) -> dict[str, Any]:
        import psycopg
        from psycopg.rows import dict_row

        with psycopg.connect(self.database_url, row_factory=dict_row) as conn:
            row = conn.execute(_SUMMARY_SQL).fetchone()
        return self._public_summary(dict(row or {}))

    def _public_summary(self, row: dict[str, Any]) -> dict[str, Any]:
        return {
            "total_runs": int(row.get("total_runs") or 0),
            "completed_runs": int(row.get("completed_runs") or 0),
            "review_needed_runs": int(row.get("review_needed_runs") or 0),
            "failed_runs": int(row.get("failed_runs") or 0),
            # Computed from the CURRENT confirmed estimate (Nick 2026-06-08,
            # ~8 min per completed as-built) so historical runs logged under
            # the old 20-min placeholder don't inflate the total. Per-run
            # stored values are kept for audit. Dollars stay hidden.
            "estimated_minutes_saved": round(
                int(row.get("savings_eligible_runs") or 0)
                * max(0.0, float(self.savings_minutes_per_completed_pdf)),
                1,
            ),
            "savings_since": SAVINGS_START_LABEL,
        }

    def _nick_review(self, summary: dict[str, Any]) -> dict[str, Any]:
        return {
            "completed_runs": summary["completed_runs"],
            "review_needed_runs": summary["review_needed_runs"],
            "failed_runs": summary["failed_runs"],
            "estimated_minutes_saved": summary["estimated_minutes_saved"],
            "savings_since": SAVINGS_START_LABEL,
            "assumption_note": (
                f"Time saved counts completed runs from {SAVINGS_START_LABEL} (production launch) "
                "at ~8 min each (Nick's 2026-06-08 estimate). "
                "Dollar savings stay hidden until the hourly rate is confirmed."
            ),
        }

    def _public_row(self, row: dict[str, Any]) -> dict[str, Any]:
        selected_extras = row.get("selected_extras_json") or "[]"
        try:
            selected = json.loads(selected_extras)
        except json.JSONDecodeError:
            selected = []
        try:
            result_lines = json.loads(row.get("result_lines_json") or "[]")
        except json.JSONDecodeError:
            result_lines = []
        duration_seconds = round(float(row.get("duration_ms") or 0) / 1000.0, 2)
        return {
            "id": row.get("id") or "",
            "created_at": _iso_string(row.get("created_at")),
            "source_filename": row.get("source_filename") or "",
            "output_filename": row.get("output_filename") or "",
            "status": row.get("status") or "",
            "duration_ms": int(row.get("duration_ms") or 0),
            "duration_seconds": duration_seconds,
            "pages_processed": row.get("pages_processed"),
            "model": row.get("model") or "",
            "confidence": row.get("confidence"),
            "detected_totals_count": int(row.get("detected_totals_count") or 0),
            "extra_billing_codes_count": int(row.get("extra_billing_codes_count") or 0),
            "selected_extras": selected,
            "warnings_count": int(row.get("warnings_count") or 0),
            "error_type": row.get("error_type") or "",
            "error_message": row.get("error_message") or "",
            "estimated_minutes_saved": round(float(row.get("estimated_minutes_saved") or 0.0), 1),
            "has_input": bool(row.get("has_input")),
            "has_output": bool(row.get("has_output")),
            "result_lines": result_lines if isinstance(result_lines, list) else [],
        }


def _iso_string(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, datetime):
        if value.tzinfo is None:
            value = value.replace(tzinfo=timezone.utc)
        return value.isoformat()
    return str(value)


_POSTGRES_SCHEMA = """
create table if not exists asbuilt_run_history (
  id text primary key,
  created_at timestamptz not null,
  source_filename text not null,
  output_filename text not null default '',
  status text not null,
  duration_ms integer not null default 0,
  pages_processed integer,
  model text not null default '',
  confidence double precision,
  detected_totals_count integer not null default 0,
  extra_billing_codes_count integer not null default 0,
  selected_extras_json text not null default '[]',
  warnings_count integer not null default 0,
  error_type text not null default '',
  error_message text not null default '',
  estimated_minutes_saved double precision not null default 0,
  estimated_dollars_saved double precision not null default 0,
  input_pdf bytea,
  output_pdf bytea,
  result_lines_json text not null default '[]'
);
create index if not exists idx_asbuilt_run_history_created_at
  on asbuilt_run_history (created_at desc);
"""

_POSTGRES_MIGRATIONS = [
    "alter table asbuilt_run_history add column if not exists input_pdf bytea",
    "alter table asbuilt_run_history add column if not exists output_pdf bytea",
    "alter table asbuilt_run_history add column if not exists result_lines_json text not null default '[]'",
]

_SQLITE_SCHEMA = """
create table if not exists asbuilt_run_history (
  id text primary key,
  created_at text not null,
  source_filename text not null,
  output_filename text not null default '',
  status text not null,
  duration_ms integer not null default 0,
  pages_processed integer,
  model text not null default '',
  confidence real,
  detected_totals_count integer not null default 0,
  extra_billing_codes_count integer not null default 0,
  selected_extras_json text not null default '[]',
  warnings_count integer not null default 0,
  error_type text not null default '',
  error_message text not null default '',
  estimated_minutes_saved real not null default 0,
  estimated_dollars_saved real not null default 0,
  input_pdf blob,
  output_pdf blob,
  result_lines_json text not null default '[]'
);
create index if not exists idx_asbuilt_run_history_created_at
  on asbuilt_run_history (created_at desc);
"""

_SQLITE_MIGRATIONS = [
    ("input_pdf", "alter table asbuilt_run_history add column input_pdf blob"),
    ("output_pdf", "alter table asbuilt_run_history add column output_pdf blob"),
    ("result_lines_json", "alter table asbuilt_run_history add column result_lines_json text not null default '[]'"),
]

_LIST_COLUMNS = (
    "id, created_at, source_filename, output_filename, status, duration_ms,"
    " pages_processed, model, confidence, detected_totals_count,"
    " extra_billing_codes_count, selected_extras_json, warnings_count,"
    " error_type, error_message, estimated_minutes_saved, result_lines_json,"
    " (input_pdf is not null) as has_input, (output_pdf is not null) as has_output"
)

_POSTGRES_INSERT_SQL = """
insert into asbuilt_run_history (
  id,
  created_at,
  source_filename,
  output_filename,
  status,
  duration_ms,
  pages_processed,
  model,
  confidence,
  detected_totals_count,
  extra_billing_codes_count,
  selected_extras_json,
  warnings_count,
  error_type,
  error_message,
  estimated_minutes_saved,
  estimated_dollars_saved,
  input_pdf,
  output_pdf,
  result_lines_json
) values (
  %(id)s,
  %(created_at)s,
  %(source_filename)s,
  %(output_filename)s,
  %(status)s,
  %(duration_ms)s,
  %(pages_processed)s,
  %(model)s,
  %(confidence)s,
  %(detected_totals_count)s,
  %(extra_billing_codes_count)s,
  %(selected_extras_json)s,
  %(warnings_count)s,
  %(error_type)s,
  %(error_message)s,
  %(estimated_minutes_saved)s,
  %(estimated_dollars_saved)s,
  %(input_pdf)s,
  %(output_pdf)s,
  %(result_lines_json)s
)
"""

_SQLITE_INSERT_SQL = _POSTGRES_INSERT_SQL.replace("%(", ":").replace(")s", "")

_SUMMARY_SQL = f"""
select
  count(*) as total_runs,
  sum(case when status in ('success', 'manual_review') and output_filename <> '' then 1 else 0 end) as completed_runs,
  sum(case when status in ('success', 'manual_review') and output_filename <> ''
        and created_at >= '{SAVINGS_START_DATE}' then 1 else 0 end) as savings_eligible_runs,
  sum(case when status = 'manual_review' then 1 else 0 end) as review_needed_runs,
  sum(case when status = 'failed' then 1 else 0 end) as failed_runs,
  sum(estimated_minutes_saved) as estimated_minutes_saved,
  sum(estimated_dollars_saved) as estimated_dollars_saved
from asbuilt_run_history
"""
