import asyncio
import json
import re
import shutil
import uuid
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Optional

from fastapi import BackgroundTasks, FastAPI, File, Form, HTTPException, UploadFile
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from .config import settings
from .db import db, now
from .models import ContinuityUpdate, QAOverrideRequest, ReferenceAssetUpdate, ShotReferencesUpdate, ShotRetryRequest, ShotRewriteRequest, ShotUpdate
from .service import backfill_legacy_continuity_plans, build_project_preview, generate_storyboard, migrate_legacy_reference_assets, provider_error_message, recheck_storyboard, rewrite_storyboard_shot, worker


@asynccontextmanager
async def lifespan(app: FastAPI):
    db.init()
    migrate_legacy_reference_assets()
    backfill_legacy_continuity_plans()
    db.execute(
        "UPDATE shots SET status='failed', error='服务在提交 H3 时中断；为避免重复生成，请人工重试', updated_at=? "
        "WHERE status='submitting'",
        (now(),),
    )
    stop = asyncio.Event()
    task = asyncio.create_task(worker(stop))
    preview_tasks = [
        asyncio.create_task(build_project_preview(row["id"]))
        for row in db.all("SELECT id FROM projects WHERE status='completed' AND preview_path IS NULL")
    ]
    yield
    stop.set()
    await task
    if preview_tasks:
        await asyncio.gather(*preview_tasks, return_exceptions=True)


app = FastAPI(title="商品图文生成短视频流水线", lifespan=lifespan)
app.mount("/static", StaticFiles(directory=Path(__file__).parent / "static"), name="static")


@app.get("/")
def index():
    return FileResponse(Path(__file__).parent / "static" / "index.html")


async def read_reference_image(image: UploadFile) -> tuple[bytes, str]:
    content = await image.read(settings.max_upload_bytes + 1)
    if len(content) > settings.max_upload_bytes:
        raise HTTPException(413, f"每张图片不能超过 {settings.max_upload_bytes // (1024 * 1024)}MB")
    if content.startswith(b"\xff\xd8\xff"):
        suffix = ".jpg"
    elif content.startswith(b"\x89PNG\r\n\x1a\n"):
        suffix = ".png"
    elif len(content) >= 12 and content[:4] == b"RIFF" and content[8:12] == b"WEBP":
        suffix = ".webp"
    else:
        raise HTTPException(400, "只支持有效的 JPEG、PNG 或 WebP 图片")
    return content, suffix


def parse_asset_metadata(raw: str, count: int) -> list[ReferenceAssetUpdate]:
    try:
        values = json.loads(raw or "[]")
        if not isinstance(values, list):
            raise ValueError
        metadata = [ReferenceAssetUpdate.model_validate(value) for value in values[:count]]
    except Exception as exc:
        raise HTTPException(400, "Reference 图片标记格式不正确") from exc
    while len(metadata) < count:
        metadata.append(ReferenceAssetUpdate())
    primary_index = next((index for index, item in enumerate(metadata) if item.is_primary), 0)
    for index, item in enumerate(metadata):
        item.is_primary = index == primary_index
        if item.is_primary:
            item.priority = "core"
    return metadata


def ensure_asset_mutable(project_id: str) -> dict:
    project = db.one("SELECT id,status FROM projects WHERE id=?", (project_id,))
    if not project:
        raise HTTPException(404, "项目不存在")
    if project["status"] in {"scripting", "generating"}:
        raise HTTPException(409, "项目正在生成，完成后才能修改 Reference Library")
    return project


@app.post("/api/projects", status_code=202)
async def create_project(background: BackgroundTasks, name: str = Form(...), selling_points: str = Form(...), image: list[UploadFile] = File(...), asset_metadata: str = Form("[]")):
    name, selling_points = name.strip(), selling_points.strip()
    if not name or len(name) > 100:
        raise HTTPException(400, "商品名长度必须为 1-100 个字符")
    if not selling_points or len(selling_points) > 4000:
        raise HTTPException(400, "卖点长度必须为 1-4000 个字符")
    if not image or len(image) > 9:
        raise HTTPException(400, "首次请上传 1-9 张 Reference 图片")
    validated = [await read_reference_image(upload) for upload in image]
    metadata = parse_asset_metadata(asset_metadata, len(image))
    pid = uuid.uuid4().hex
    upload_dir = settings.data_dir / "uploads"
    upload_dir.mkdir(parents=True, exist_ok=True)
    reference_dir = settings.data_dir / "references" / pid
    reference_dir.mkdir(parents=True, exist_ok=True)
    paths = []
    asset_ids = [uuid.uuid4().hex for _ in image]
    for index, ((content, suffix), asset_id) in enumerate(zip(validated, asset_ids)):
        path = upload_dir / f"{pid}{suffix}" if index == 0 else reference_dir / f"{asset_id}{suffix}"
        path.write_bytes(content)
        paths.append(path)
    primary_index = next(index for index, item in enumerate(metadata) if item.is_primary)
    timestamp = now()
    with db.conn() as con:
        con.execute(
            "INSERT INTO projects (id,name,selling_points,image_path,status,script_json,qa_json,confirmed_at,created_at,updated_at) VALUES (?,?,?,?,?,?,?,?,?,?)",
            (pid, name, selling_points, str(paths[primary_index]), "scripting", None, None, None, timestamp, timestamp),
        )
        for index, (upload, path, asset_id, item) in enumerate(zip(image, paths, asset_ids, metadata)):
            con.execute(
                "INSERT INTO reference_assets (id,project_id,file_path,original_filename,asset_type,subject_name,view_tags_json,priority,description,constraints_text,is_primary,created_at,updated_at) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (asset_id, pid, str(path), upload.filename or f"reference-{index + 1}{path.suffix}", item.asset_type,
                 item.subject_name or name, json.dumps(item.view_tags, ensure_ascii=False), item.priority,
                 item.description, item.constraints, int(item.is_primary), timestamp, timestamp),
            )
    background.add_task(generate_storyboard, pid)
    return {"id": pid, "status": "scripting"}


@app.get("/api/projects")
def list_projects():
    rows = db.all(
        "SELECT p.id,p.name,p.status,p.qa_json,p.created_at,p.updated_at,COUNT(s.id) AS shot_count "
        "FROM projects p LEFT JOIN shots s ON s.project_id=p.id "
        "GROUP BY p.id ORDER BY p.updated_at DESC LIMIT 50"
    )
    for row in rows:
        qa = json.loads(row.pop("qa_json")) if row.get("qa_json") else None
        row["qa_score"] = qa.get("score") if qa else None
        row["qa_passed"] = qa.get("passed") if qa else None
    return rows


@app.delete("/api/projects/{project_id}")
def delete_project(project_id: str):
    project = db.one("SELECT image_path FROM projects WHERE id=?", (project_id,))
    if not project:
        raise HTTPException(404, "项目不存在")
    owned_asset_paths = [row["file_path"] for row in db.all(
        "SELECT file_path FROM reference_assets WHERE project_id=?", (project_id,)
    )]
    with db.conn() as con:
        con.execute("DELETE FROM reference_assets WHERE project_id=?", (project_id,))
        con.execute("DELETE FROM shots WHERE project_id=?", (project_id,))
        deleted = con.execute("DELETE FROM projects WHERE id=?", (project_id,)).rowcount
        if not deleted:
            raise HTTPException(409, "项目状态已变化")

    # Database deletion is authoritative. Files are removed only when they are
    # inside the expected project-owned directories.
    cleanup_warnings = []
    upload_root = (settings.data_dir / "uploads").resolve()
    reference_root = (settings.data_dir / "references").resolve()
    for raw_path in set(owned_asset_paths + [project["image_path"]]):
        image_path = Path(raw_path).resolve()
        try:
            if (upload_root in image_path.parents or reference_root in image_path.parents) and image_path.is_file():
                image_path.unlink()
        except OSError as exc:
            cleanup_warnings.append(f"Reference 图片清理失败：{exc}")
    video_root = (settings.data_dir / "videos").resolve()
    video_dir = (video_root / project_id).resolve()
    try:
        if video_dir.parent == video_root and video_dir.is_dir():
            shutil.rmtree(video_dir)
    except OSError as exc:
        cleanup_warnings.append(f"视频缓存清理失败：{exc}")
    reference_dir = (reference_root / project_id).resolve()
    try:
        if reference_dir.parent == reference_root and reference_dir.is_dir():
            shutil.rmtree(reference_dir)
    except OSError as exc:
        cleanup_warnings.append(f"Reference 图片清理失败：{exc}")
    for folder_name in ("previews", "keyframes"):
        folder_root = (settings.data_dir / folder_name).resolve()
        candidate = (folder_root / project_id).resolve() if folder_name == "keyframes" else (folder_root / f"{project_id}.mp4").resolve()
        try:
            if folder_name == "keyframes" and candidate.parent == folder_root and candidate.is_dir():
                shutil.rmtree(candidate)
            elif folder_name == "previews" and candidate.parent == folder_root and candidate.is_file():
                candidate.unlink()
        except OSError as exc:
            cleanup_warnings.append(f"{folder_name} 清理失败：{exc}")
    result = {"deleted": True, "id": project_id}
    if cleanup_warnings:
        result["cleanup_warnings"] = cleanup_warnings
    return result


@app.get("/api/projects/{project_id}")
def get_project(project_id: str):
    p = db.project(project_id)
    if not p:
        raise HTTPException(404, "项目不存在")
    p.pop("image_path", None)
    preview_eligible = (
        p["preview_ready"]
        and p["status"] == "completed"
        and bool(p["confirmed_at"])
        and bool(p["shots"])
        and all(shot["status"] == "succeeded" for shot in p["shots"])
    )
    p["preview_ready"] = preview_eligible
    if preview_eligible:
        p["preview_url"] = f"/api/projects/{project_id}/preview"
        p["export_url"] = f"/api/projects/{project_id}/export"
    for shot in p["shots"]:
        if shot["video_cached"]:
            shot["local_video_url"] = f"/api/projects/{project_id}/shots/{shot['id']}/video"
    for asset in p["reference_assets"]:
        asset.pop("file_path", None)
        asset["url"] = f"/api/projects/{project_id}/references/{asset['id']}/image"
    return p


@app.get("/api/projects/{project_id}/references/{asset_id}/image")
def get_reference_image(project_id: str, asset_id: str):
    asset = db.one("SELECT file_path FROM reference_assets WHERE id=? AND project_id=?", (asset_id, project_id))
    if not asset:
        raise HTTPException(404, "Reference 图片不存在")
    path = Path(asset["file_path"]).resolve()
    allowed_roots = [(settings.data_dir / "uploads").resolve(), (settings.data_dir / "references").resolve()]
    if not path.is_file() or not any(root in path.parents for root in allowed_roots):
        raise HTTPException(404, "Reference 图片文件不存在")
    return FileResponse(path)


@app.post("/api/projects/{project_id}/references", status_code=201)
async def add_reference_asset(
    project_id: str, image: UploadFile = File(...), asset_type: str = Form("main_product"),
    subject_name: str = Form(""), view_tags: str = Form("[]"), priority: str = Form("supporting"),
    description: str = Form(""), constraints: str = Form(""), is_primary: bool = Form(False),
):
    ensure_asset_mutable(project_id)
    if len(db.all("SELECT id FROM reference_assets WHERE project_id=?", (project_id,))) >= 9:
        raise HTTPException(409, "每个项目最多保存 9 张 Reference 图片")
    try:
        tags = json.loads(view_tags)
        item = ReferenceAssetUpdate.model_validate({"asset_type": asset_type, "subject_name": subject_name,
            "view_tags": tags, "priority": priority, "description": description,
            "constraints": constraints, "is_primary": is_primary})
    except Exception as exc:
        raise HTTPException(400, "Reference 图片标记格式不正确") from exc
    content, suffix = await read_reference_image(image)
    asset_id, timestamp = uuid.uuid4().hex, now()
    folder = settings.data_dir / "references" / project_id
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / f"{asset_id}{suffix}"
    path.write_bytes(content)
    with db.conn() as con:
        if item.is_primary:
            con.execute("UPDATE reference_assets SET is_primary=0 WHERE project_id=?", (project_id,))
        con.execute(
            "INSERT INTO reference_assets (id,project_id,file_path,original_filename,asset_type,subject_name,view_tags_json,priority,description,constraints_text,is_primary,created_at,updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (asset_id, project_id, str(path), image.filename or path.name, item.asset_type, item.subject_name,
             json.dumps(item.view_tags, ensure_ascii=False), "core" if item.is_primary else item.priority,
             item.description, item.constraints, int(item.is_primary), timestamp, timestamp),
        )
        if item.is_primary:
            con.execute("UPDATE projects SET image_path=?,updated_at=? WHERE id=?", (str(path), timestamp, project_id))
        else:
            con.execute("UPDATE projects SET updated_at=? WHERE id=?", (timestamp, project_id))
    return {"id": asset_id, "url": f"/api/projects/{project_id}/references/{asset_id}/image"}


@app.patch("/api/projects/{project_id}/references/{asset_id}")
def update_reference_asset(project_id: str, asset_id: str, update: ReferenceAssetUpdate):
    ensure_asset_mutable(project_id)
    asset = db.one("SELECT file_path FROM reference_assets WHERE id=? AND project_id=?", (asset_id, project_id))
    if not asset:
        raise HTTPException(404, "Reference 图片不存在")
    timestamp = now()
    with db.conn() as con:
        if update.is_primary:
            con.execute("UPDATE reference_assets SET is_primary=0 WHERE project_id=?", (project_id,))
        con.execute(
            "UPDATE reference_assets SET asset_type=?,subject_name=?,view_tags_json=?,priority=?,description=?,constraints_text=?,is_primary=?,updated_at=? WHERE id=? AND project_id=?",
            (update.asset_type, update.subject_name, json.dumps(update.view_tags, ensure_ascii=False),
             "core" if update.is_primary else update.priority, update.description, update.constraints,
             int(update.is_primary), timestamp, asset_id, project_id),
        )
        if update.is_primary:
            con.execute("UPDATE projects SET image_path=?,updated_at=? WHERE id=?", (asset["file_path"], timestamp, project_id))
        else:
            primary = con.execute("SELECT id FROM reference_assets WHERE project_id=? AND is_primary=1", (project_id,)).fetchone()
            if not primary:
                raise HTTPException(409, "项目必须保留一张主参考图")
            con.execute("UPDATE projects SET updated_at=? WHERE id=?", (timestamp, project_id))
    return {"updated": True, "id": asset_id}


@app.delete("/api/projects/{project_id}/references/{asset_id}")
def delete_reference_asset(project_id: str, asset_id: str):
    ensure_asset_mutable(project_id)
    assets = db.all("SELECT * FROM reference_assets WHERE project_id=? ORDER BY is_primary DESC,created_at", (project_id,))
    asset = next((item for item in assets if item["id"] == asset_id), None)
    if not asset:
        raise HTTPException(404, "Reference 图片不存在")
    if len(assets) == 1:
        raise HTTPException(409, "项目必须至少保留一张 Reference 图片")
    replacement = next(item for item in assets if item["id"] != asset_id)
    timestamp = now()
    with db.conn() as con:
        con.execute("DELETE FROM reference_assets WHERE id=? AND project_id=?", (asset_id, project_id))
        if asset["is_primary"]:
            con.execute("UPDATE reference_assets SET is_primary=1,priority='core',updated_at=? WHERE id=?", (timestamp, replacement["id"]))
            con.execute("UPDATE projects SET image_path=?,updated_at=? WHERE id=?", (replacement["file_path"], timestamp, project_id))
        else:
            con.execute("UPDATE projects SET updated_at=? WHERE id=?", (timestamp, project_id))
        project = con.execute("SELECT script_json FROM projects WHERE id=?", (project_id,)).fetchone()
        if project and project["script_json"]:
            script = json.loads(project["script_json"])
            for plan in script.get("shots", []):
                plan["reference_asset_ids"] = [value for value in plan.get("reference_asset_ids", []) if value != asset_id]
                if not plan["reference_asset_ids"]:
                    plan["reference_asset_ids"] = [replacement["id"]]
                    plan["reference_source"] = "system"
            con.execute("UPDATE projects SET script_json=? WHERE id=?", (json.dumps(script, ensure_ascii=False), project_id))
    path = Path(asset["file_path"]).resolve()
    reference_root = (settings.data_dir / "references").resolve()
    upload_root = (settings.data_dir / "uploads").resolve()
    cleanup_warning = None
    try:
        if path.is_file() and (reference_root in path.parents or upload_root in path.parents):
            path.unlink()
    except OSError as exc:
        cleanup_warning = f"数据库记录已删除，但图片文件清理失败：{exc}"
    result = {"deleted": True, "id": asset_id}
    if cleanup_warning:
        result["cleanup_warning"] = cleanup_warning
    return result


@app.get("/api/projects/{project_id}/preview")
def get_project_preview(project_id: str):
    project = db.one(
        "SELECT status,confirmed_at,preview_path FROM projects WHERE id=?",
        (project_id,),
    )
    if (not project or project["status"] != "completed" or
            not project["confirmed_at"] or not project["preview_path"]):
        raise HTTPException(404, "合成预览尚未生成")
    if (not db.one("SELECT id FROM shots WHERE project_id=? LIMIT 1", (project_id,)) or db.one(
        "SELECT id FROM shots WHERE project_id=? AND status!='succeeded' LIMIT 1",
        (project_id,),
    )):
        raise HTTPException(404, "合成预览尚未生成")
    path = Path(project["preview_path"]).resolve()
    preview_root = (settings.data_dir / "previews").resolve()
    if path.parent != preview_root or not path.is_file():
        raise HTTPException(404, "合成预览不存在")
    return FileResponse(path, media_type="video/mp4")


@app.get("/api/projects/{project_id}/export")
def export_project_video(project_id: str):
    project = db.one(
        "SELECT name,status,confirmed_at,preview_path FROM projects WHERE id=?",
        (project_id,),
    )
    if (not project or project["status"] != "completed" or
            not project["confirmed_at"] or not project["preview_path"]):
        raise HTTPException(404, "完整成片尚未生成")
    if (not db.one("SELECT id FROM shots WHERE project_id=? LIMIT 1", (project_id,)) or db.one(
        "SELECT id FROM shots WHERE project_id=? AND status!='succeeded' LIMIT 1",
        (project_id,),
    )):
        raise HTTPException(404, "完整成片尚未生成")
    path = Path(project["preview_path"]).resolve()
    preview_root = (settings.data_dir / "previews").resolve()
    if path.parent != preview_root or not path.is_file():
        raise HTTPException(404, "完整成片不存在")
    safe_name = re.sub(r'[\\/:*?"<>|\r\n]+', "-", project["name"]).strip(" .-")[:80]
    filename = f"{safe_name or 'frameflow-video'}.mp4"
    return FileResponse(path, media_type="video/mp4", filename=filename)


@app.get("/api/projects/{project_id}/shots/{shot_id}/video")
def get_cached_video(project_id: str, shot_id: str):
    shot = db.one(
        "SELECT local_video_path FROM shots WHERE id=? AND project_id=?",
        (shot_id, project_id),
    )
    if not shot or not shot["local_video_path"]:
        raise HTTPException(404, "本地视频不存在")
    path = Path(shot["local_video_path"]).resolve()
    video_root = (settings.data_dir / "videos").resolve()
    if video_root not in path.parents or not path.is_file():
        raise HTTPException(404, "本地视频不存在")
    return FileResponse(path, media_type="video/mp4")


@app.patch("/api/projects/{project_id}/shots/{shot_id}")
async def update_shot(project_id: str, shot_id: str, update: ShotUpdate):
    timestamp = now()
    with db.conn() as con:
        shot = con.execute(
            "SELECT position,status FROM shots WHERE id=? AND project_id=?",
            (shot_id, project_id),
        ).fetchone()
        project = con.execute(
            "SELECT status,confirmed_at,script_json FROM projects WHERE id=?",
            (project_id,),
        ).fetchone()
        if not shot:
            raise HTTPException(404, "镜头不存在")
        editable_draft = (
            project and not project["confirmed_at"]
            and project["status"] in {"awaiting_confirmation", "qa_failed", "qa_stale"}
            and shot["status"] == "draft"
        )
        editable_generated = (
            project and project["confirmed_at"]
            and shot["status"] in {"failed", "succeeded", "revision_pending"}
        )
        if not editable_draft and not editable_generated:
            raise HTTPException(409, "只能修改草稿、生成失败或已完成的镜头")
        script = json.loads(project["script_json"])
        plan = next(
            (item for item in script.get("shots", []) if item.get("position") == shot["position"]),
            None,
        )
        if not plan:
            raise HTTPException(409, "分镜计划不存在")
        plan.update({
            "title": update.title,
            "duration": update.duration,
            "prompt": update.prompt,
            "voiceover": update.voiceover,
            "overlay_text": update.overlay_text,
        })
        for key in ("transition_type", "transition_duration_ms", "entry_action", "exit_action", "continuity_group"):
            value = getattr(update, key)
            if value is not None:
                plan[key] = value or (None if key == "continuity_group" else "")
        generated_note = "分镜结构已修改，当前视频仍是修改前版本；请重新生成本镜头。" if editable_generated else None
        new_shot_status = "revision_pending" if editable_generated else "draft"
        changed = con.execute(
            "UPDATE shots SET title=?,duration=?,prompt=?,voiceover=?,overlay_text=?,status=?,error=?,updated_at=? "
            "WHERE id=? AND project_id=? AND status=?",
            (update.title, update.duration, update.prompt, update.voiceover, update.overlay_text,
             new_shot_status, generated_note, timestamp, shot_id, project_id, shot["status"]),
        ).rowcount
        if not changed:
            raise HTTPException(409, "镜头状态已变化")
        if editable_draft:
            con.execute(
                "UPDATE projects SET script_json=?,status='qa_stale',"
                "qa_overridden_at=NULL,qa_override_reason=NULL,updated_at=? WHERE id=?",
                (json.dumps(script, ensure_ascii=False), timestamp, project_id),
            )
        else:
            con.execute(
                "UPDATE projects SET script_json=?,status='revision_pending',"
                "preview_path=NULL,preview_error=NULL,updated_at=? WHERE id=?",
                (json.dumps(script, ensure_ascii=False), timestamp, project_id),
            )
    if editable_draft:
        return {"status": "draft_saved", "qa_stale": True, "requires_regeneration": False}
    return {"status": "generated_plan_saved", "qa_stale": False, "requires_regeneration": True}


@app.post("/api/projects/{project_id}/shots/{shot_id}/rewrite-script")
async def rewrite_shot_script(project_id: str, shot_id: str, request: ShotRewriteRequest):
    try:
        rewritten = await rewrite_storyboard_shot(project_id, shot_id, request.instruction)
    except KeyError as exc:
        raise HTTPException(404, "镜头不存在") from exc
    except ValueError as exc:
        raise HTTPException(409, str(exc)) from exc
    except Exception as exc:
        raise HTTPException(502, provider_error_message(exc, "M3 单镜重写")) from exc
    return {"status": "qa_stale", "shot": rewritten.model_dump()}


@app.post("/api/projects/{project_id}/recheck-script")
async def recheck_script(project_id: str):
    if not db.one("SELECT id FROM projects WHERE id=?", (project_id,)):
        raise HTTPException(404, "项目不存在")
    try:
        qa = await recheck_storyboard(project_id)
    except ValueError as exc:
        raise HTTPException(409, str(exc)) from exc
    return {"status": "awaiting_confirmation" if qa.passed else "qa_failed", "qa": qa}


@app.patch("/api/projects/{project_id}/shots/{shot_id}/continuity")
def update_shot_continuity(project_id: str, shot_id: str, update: ContinuityUpdate):
    """Persist the human tail-frame choice for the next generation of this shot."""
    timestamp = now()
    with db.conn() as con:
        shot = con.execute(
            "SELECT position,status FROM shots WHERE id=? AND project_id=?",
            (shot_id, project_id),
        ).fetchone()
        project = con.execute(
            "SELECT script_json,confirmed_at FROM projects WHERE id=?",
            (project_id,),
        ).fetchone()
        if not shot or not project:
            raise HTTPException(404, "镜头不存在")
        if shot["position"] == 1 and update.enabled:
            raise HTTPException(409, "第一镜没有上一镜，不能插入尾帧")
        if shot["status"] not in {"draft", "failed", "succeeded", "revision_pending"}:
            raise HTTPException(409, "镜头正在生成，完成后才能调整尾帧衔接")
        if not project["script_json"]:
            raise HTTPException(409, "分镜脚本尚未生成")

        script = json.loads(project["script_json"])
        plans = {item.get("position"): item for item in script.get("shots", [])}
        plan = plans.get(shot["position"])
        if not plan:
            raise HTTPException(409, "分镜计划不存在")
        plan["continuity_mode"] = "carry_last_frame" if update.enabled else "independent"
        plan["continuity_source"] = "human"
        if update.enabled:
            previous = plans.get(shot["position"] - 1)
            if not previous:
                raise HTTPException(409, "找不到上一镜，无法建立尾帧衔接")
            group = previous.get("continuity_group") or plan.get("continuity_group") or f"shots-{shot['position'] - 1}-{shot['position']}"
            previous["continuity_group"] = group
            plan["continuity_group"] = group
            cursor = shot["position"] - 1
            while cursor > 1 and plans.get(cursor, {}).get("continuity_mode") == "carry_last_frame":
                plans[cursor - 1]["continuity_group"] = group
                plans[cursor]["continuity_group"] = group
                cursor -= 1
            cursor = shot["position"] + 1
            while plans.get(cursor, {}).get("continuity_mode") == "carry_last_frame":
                plans[cursor]["continuity_group"] = group
                cursor += 1
        con.execute(
            "UPDATE projects SET script_json=?, updated_at=? WHERE id=?",
            (json.dumps(script, ensure_ascii=False), timestamp, project_id),
        )

    return {
        "enabled": update.enabled,
        "continuity_mode": plan["continuity_mode"],
        "continuity_source": "human",
        "applies_to": "next_generation" if project["confirmed_at"] else "confirmation",
    }


@app.patch("/api/projects/{project_id}/shots/{shot_id}/references")
def update_shot_references(project_id: str, shot_id: str, update: ShotReferencesUpdate):
    timestamp = now()
    asset_ids = list(dict.fromkeys(update.asset_ids))
    with db.conn() as con:
        shot = con.execute(
            "SELECT position,status FROM shots WHERE id=? AND project_id=?", (shot_id, project_id)
        ).fetchone()
        project = con.execute("SELECT script_json,confirmed_at FROM projects WHERE id=?", (project_id,)).fetchone()
        if not shot or not project:
            raise HTTPException(404, "镜头不存在")
        if shot["status"] not in {"draft", "failed", "succeeded", "revision_pending"}:
            raise HTTPException(409, "镜头正在生成，完成后才能调整 Reference 图片")
        rows = con.execute(
            f"SELECT id FROM reference_assets WHERE project_id=? AND id IN ({','.join('?' for _ in asset_ids)})",
            (project_id, *asset_ids),
        ).fetchall()
        if {row["id"] for row in rows} != set(asset_ids):
            raise HTTPException(400, "包含不属于当前项目的 Reference 图片")
        if not project["script_json"]:
            raise HTTPException(409, "分镜脚本尚未生成")
        script = json.loads(project["script_json"])
        plan = next((item for item in script.get("shots", []) if item.get("position") == shot["position"]), None)
        if not plan:
            raise HTTPException(409, "分镜计划不存在")
        plan["reference_asset_ids"] = asset_ids
        plan["reference_source"] = "human"
        con.execute(
            "UPDATE projects SET script_json=?,updated_at=? WHERE id=?",
            (json.dumps(script, ensure_ascii=False), timestamp, project_id),
        )
    return {"asset_ids": asset_ids, "reference_source": "human",
            "applies_to": "next_generation" if project["confirmed_at"] else "confirmation"}


@app.post("/api/projects/{project_id}/regenerate-script", status_code=202)
async def regenerate(project_id: str, background: BackgroundTasks):
    if not db.one("SELECT id FROM projects WHERE id=?", (project_id,)):
        raise HTTPException(404, "项目不存在")
    changed = db.execute(
        "UPDATE projects SET status='scripting', qa_json=NULL, qa_overridden_at=NULL, qa_override_reason=NULL, preview_path=NULL, preview_error=NULL, updated_at=? "
        "WHERE id=? AND status IN ('awaiting_confirmation','qa_failed','qa_stale','script_failed') AND confirmed_at IS NULL",
        (now(), project_id),
    )
    if not changed:
        raise HTTPException(409, "只有尚未确认且当前未生成脚本的项目可以重做分镜")
    background.add_task(generate_storyboard, project_id)
    return {"status": "scripting"}


@app.post("/api/projects/{project_id}/override-qa")
def override_qa(project_id: str, request: QAOverrideRequest):
    p = db.project(project_id)
    if not p:
        raise HTTPException(404, "项目不存在")
    if p["status"] != "qa_failed" or not p["qa"] or p["qa"]["passed"]:
        raise HTTPException(409, "只有质量检查未通过的脚本可以人工采纳")
    reason = request.reason.strip()
    if len(reason) < 2:
        raise HTTPException(400, "请填写人工采纳原因")
    timestamp = now()
    changed = db.execute(
        "UPDATE projects SET status='awaiting_confirmation', qa_overridden_at=?, qa_override_reason=?, updated_at=? "
        "WHERE id=? AND status='qa_failed' AND confirmed_at IS NULL",
        (timestamp, reason, timestamp, project_id),
    )
    if not changed:
        raise HTTPException(409, "项目状态已变化")
    return {"status": "awaiting_confirmation", "qa_overridden": True}


@app.post("/api/projects/{project_id}/confirm")
def confirm(project_id: str):
    p = db.project(project_id)
    if not p:
        raise HTTPException(404, "项目不存在")
    if not p["qa"] or (not p["qa"]["passed"] and not p.get("qa_overridden_at")):
        raise HTTPException(409, "质量检查未通过，不能提交 H3")
    if p["status"] != "awaiting_confirmation":
        raise HTTPException(409, f"当前状态 {p['status']} 不能确认")
    timestamp = now()
    with db.conn() as con:
        changed = con.execute(
            "UPDATE projects SET status='generating', confirmed_at=?, preview_path=NULL, preview_error=NULL, updated_at=? "
            "WHERE id=? AND status='awaiting_confirmation' AND confirmed_at IS NULL",
            (timestamp, timestamp, project_id),
        ).rowcount
        if not changed:
            raise HTTPException(409, "项目已被确认或状态已变化")
        queued = con.execute("UPDATE shots SET status='queued', updated_at=? WHERE project_id=? AND status='draft'", (timestamp, project_id)).rowcount
        if not queued:
            raise HTTPException(409, "项目没有可生成的分镜")
    return {"status": "generating"}


@app.post("/api/projects/{project_id}/resume")
def resume(project_id: str):
    p = db.project(project_id)
    if not p:
        raise HTTPException(404, "项目不存在")
    if not p["confirmed_at"]:
        raise HTTPException(409, "请先确认分镜")
    timestamp = now()
    with db.conn() as con:
        queued = con.execute(
            "UPDATE shots SET status='queued', provider_task_id=NULL, output_url=NULL, local_video_path=NULL, video_qa_json=NULL, error=NULL, updated_at=? "
            "WHERE project_id=? AND status='failed' AND attempts < ?",
            (timestamp, project_id, settings.max_shot_attempts),
        ).rowcount
        if not queued:
            raise HTTPException(409, "没有可续跑的失败镜头，或镜头已达到最大尝试次数")
        con.execute("UPDATE projects SET status='generating', preview_path=NULL, preview_error=NULL, updated_at=? WHERE id=?", (timestamp, project_id))
    return {"status": "generating"}


@app.post("/api/projects/{project_id}/shots/{shot_id}/retry")
def retry_shot(project_id: str, shot_id: str, request: Optional[ShotRetryRequest] = None):
    shot = db.one("SELECT * FROM shots WHERE id=? AND project_id=?", (shot_id, project_id))
    if not shot:
        raise HTTPException(404, "镜头不存在")
    p = db.one("SELECT confirmed_at FROM projects WHERE id=?", (project_id,))
    if not p or not p["confirmed_at"]:
        raise HTTPException(409, "请先确认分镜")
    if shot["status"] not in {"failed", "succeeded", "revision_pending"}:
        raise HTTPException(409, "只能重试失败或已生成的镜头")
    if shot["attempts"] >= settings.max_shot_attempts:
        raise HTTPException(409, "镜头已达到最大尝试次数")
    prompt = request.prompt.strip() if request and request.prompt is not None else shot["prompt"]
    if len(prompt) < 10:
        raise HTTPException(400, "Prompt 至少需要 10 个字符")
    timestamp = now()
    with db.conn() as con:
        changed = con.execute(
            "UPDATE shots SET prompt=?, status='queued', provider_task_id=NULL, output_url=NULL, local_video_path=NULL, video_qa_json=NULL, error=NULL, updated_at=? "
            "WHERE id=? AND status IN ('failed','succeeded','revision_pending') AND attempts < ?",
            (prompt, timestamp, shot_id, settings.max_shot_attempts),
        ).rowcount
        if not changed:
            raise HTTPException(409, "镜头状态已变化")
        project_row = con.execute("SELECT script_json FROM projects WHERE id=?", (project_id,)).fetchone()
        script_json = project_row["script_json"] if project_row else None
        if script_json:
            script = json.loads(script_json)
            for planned_shot in script.get("shots", []):
                if planned_shot.get("position") == shot["position"]:
                    planned_shot["prompt"] = prompt
                    break
            script_json = json.dumps(script, ensure_ascii=False)
        con.execute(
            "UPDATE projects SET status='generating', script_json=COALESCE(?,script_json), preview_path=NULL, preview_error=NULL, updated_at=? WHERE id=?",
            (script_json, timestamp, project_id),
        )
    return {"status": "queued", "prompt": prompt}
