import json
import sqlite3
import threading
from contextlib import contextmanager
from datetime import datetime, timezone

from .config import settings


def now() -> str:
    return datetime.now(timezone.utc).isoformat()


class Database:
    def __init__(self):
        settings.data_dir.mkdir(parents=True, exist_ok=True)
        self.path = settings.data_dir / "pipeline.db"
        self.lock = threading.RLock()

    @contextmanager
    def conn(self):
        with self.lock:
            db = sqlite3.connect(self.path)
            db.row_factory = sqlite3.Row
            try:
                yield db
                db.commit()
            finally:
                db.close()

    def init(self):
        with self.conn() as db:
            db.executescript("""
            CREATE TABLE IF NOT EXISTS projects (
              id TEXT PRIMARY KEY, name TEXT NOT NULL, selling_points TEXT NOT NULL,
              image_path TEXT NOT NULL, status TEXT NOT NULL, script_json TEXT,
              qa_json TEXT, confirmed_at TEXT, created_at TEXT NOT NULL, updated_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS shots (
              id TEXT PRIMARY KEY, project_id TEXT NOT NULL, position INTEGER NOT NULL,
              title TEXT NOT NULL, duration INTEGER NOT NULL, prompt TEXT NOT NULL,
              voiceover TEXT NOT NULL, overlay_text TEXT NOT NULL, status TEXT NOT NULL,
              attempts INTEGER NOT NULL DEFAULT 0, provider_task_id TEXT,
              output_url TEXT, error TEXT, created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
              FOREIGN KEY(project_id) REFERENCES projects(id)
            );
            CREATE UNIQUE INDEX IF NOT EXISTS idx_shot_position ON shots(project_id, position);
            CREATE TABLE IF NOT EXISTS reference_assets (
              id TEXT PRIMARY KEY, project_id TEXT NOT NULL, file_path TEXT NOT NULL,
              original_filename TEXT NOT NULL, asset_type TEXT NOT NULL,
              subject_name TEXT NOT NULL DEFAULT '', view_tags_json TEXT NOT NULL DEFAULT '[]',
              priority TEXT NOT NULL DEFAULT 'supporting', description TEXT NOT NULL DEFAULT '',
              constraints_text TEXT NOT NULL DEFAULT '', is_primary INTEGER NOT NULL DEFAULT 0,
              created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
              FOREIGN KEY(project_id) REFERENCES projects(id)
            );
            CREATE INDEX IF NOT EXISTS idx_reference_assets_project ON reference_assets(project_id, created_at);
            """)
            project_columns = {row[1] for row in db.execute("PRAGMA table_info(projects)")}
            if "qa_overridden_at" not in project_columns:
                db.execute("ALTER TABLE projects ADD COLUMN qa_overridden_at TEXT")
            if "qa_override_reason" not in project_columns:
                db.execute("ALTER TABLE projects ADD COLUMN qa_override_reason TEXT")
            if "preview_path" not in project_columns:
                db.execute("ALTER TABLE projects ADD COLUMN preview_path TEXT")
            if "preview_error" not in project_columns:
                db.execute("ALTER TABLE projects ADD COLUMN preview_error TEXT")
            shot_columns = {row[1] for row in db.execute("PRAGMA table_info(shots)")}
            if "local_video_path" not in shot_columns:
                db.execute("ALTER TABLE shots ADD COLUMN local_video_path TEXT")
            if "video_qa_json" not in shot_columns:
                db.execute("ALTER TABLE shots ADD COLUMN video_qa_json TEXT")

    def execute(self, sql, params=()):
        with self.conn() as db:
            cursor = db.execute(sql, params)
            return cursor.rowcount

    def one(self, sql, params=()):
        with self.conn() as db:
            row = db.execute(sql, params).fetchone()
            return dict(row) if row else None

    def all(self, sql, params=()):
        with self.conn() as db:
            return [dict(r) for r in db.execute(sql, params).fetchall()]

    def project(self, project_id):
        p = self.one("SELECT * FROM projects WHERE id=?", (project_id,))
        if not p:
            return None
        p["script"] = json.loads(p.pop("script_json")) if p.get("script_json") else None
        p["qa"] = json.loads(p.pop("qa_json")) if p.get("qa_json") else None
        p["preview_ready"] = bool(p.pop("preview_path", None))
        shots = self.all("SELECT * FROM shots WHERE project_id=? ORDER BY position", (project_id,))
        plans = {item["position"]: item for item in (p["script"] or {}).get("shots", [])}
        for shot in shots:
            shot["video_qa"] = json.loads(shot.pop("video_qa_json")) if shot.get("video_qa_json") else None
            shot["video_cached"] = bool(shot.pop("local_video_path", None))
            plan = plans.get(shot["position"], {})
            for key in ("continuity_mode", "continuity_source", "transition_type", "transition_duration_ms", "entry_action", "exit_action", "continuity_group", "reference_asset_ids", "reference_source"):
                shot[key] = plan.get(key)
        assets = self.all(
            "SELECT * FROM reference_assets WHERE project_id=? ORDER BY is_primary DESC, created_at",
            (project_id,),
        )
        for asset in assets:
            asset["view_tags"] = json.loads(asset.pop("view_tags_json") or "[]")
            asset["constraints"] = asset.pop("constraints_text")
            asset["is_primary"] = bool(asset["is_primary"])
        p["reference_assets"] = assets
        p["shots"] = shots
        return p


db = Database()
