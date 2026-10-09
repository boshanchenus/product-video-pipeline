import asyncio
import json
import re
import uuid
from pathlib import Path
from typing import Optional

import httpx

from .config import settings
from .db import db, now
from .models import QAIssue, QAReport, Storyboard, VideoQAReport
from .providers import h3_provider, m3_provider
from .quality import deterministic_qa, merge_qa


def provider_error_message(exc: Exception, operation: str) -> str:
    """Return a useful error even for exceptions whose str() is empty."""
    kind = type(exc).__name__
    detail = str(exc).strip()
    if isinstance(exc, httpx.TimeoutException):
        return (
            f"{operation}请求超时（{settings.m3_timeout_seconds:g} 秒，{kind}）。"
            "请检查网络后重试；参考图片较多或文件较大时，可先减少或压缩图片。"
        )
    if isinstance(exc, httpx.HTTPStatusError):
        body = exc.response.text.strip()[:450]
        suffix = body or detail or "接口未提供错误详情"
        return f"{operation}接口返回 HTTP {exc.response.status_code}（{kind}）：{suffix}"
    if isinstance(exc, json.JSONDecodeError):
        return f"{operation}返回的 JSON 无法解析（{kind}）：{detail or '接口未提供错误详情'}"
    return f"{operation}失败（{kind}）：{detail or '异常未提供详细信息'}"


def prompt_uses_chinese(text: str) -> bool:
    chinese = len(re.findall(r"[\u3400-\u9fff]", text or ""))
    latin = len(re.findall(r"[A-Za-z]", text or ""))
    return chinese >= 4 and chinese * 2 >= latin


def same_prompt_language(text: str, chinese: bool) -> bool:
    if not text:
        return True
    has_chinese = bool(re.search(r"[\u3400-\u9fff]", text))
    return has_chinese == chinese


def continuity_copy(shot, chinese: bool) -> str:
    entry = shot.entry_action if same_prompt_language(shot.entry_action, chinese) else ""
    exit_action = shot.exit_action if same_prompt_language(shot.exit_action, chinese) else ""
    if chinese:
        carry = "从提供的首帧自然延续，不得突变主体、场景、光线与运动方向；" if shot.continuity_mode == "carry_last_frame" else ""
        entry = entry or ("以干净的硬切进入既定构图" if shot.transition_type == "cut" else "自然承接既定构图与动作")
        exit_action = exit_action or "以稳定画面和自然动作收束，便于下一次剪辑"
        return f"镜头衔接：{carry}开场动作：{entry}；收尾动作：{exit_action}。"
    carry = "Continue naturally from the supplied first frame without changing the subject, setting, lighting, or direction of motion. " if shot.continuity_mode == "carry_last_frame" else ""
    entry = entry or ("Open with a clean hard cut into the planned composition" if shot.transition_type == "cut" else "Open by naturally continuing the planned composition and motion")
    exit_action = exit_action or "End on a stable composition with motion resolved for the next edit"
    return f"Shot continuity: {carry}Opening action: {entry}. Closing action: {exit_action}."


def reference_assets(project_id: str) -> list[dict]:
    assets = db.all(
        "SELECT * FROM reference_assets WHERE project_id=? ORDER BY is_primary DESC, created_at",
        (project_id,),
    )
    for asset in assets:
        asset["view_tags"] = json.loads(asset.pop("view_tags_json") or "[]")
        asset["constraints"] = asset.pop("constraints_text")
        asset["is_primary"] = bool(asset["is_primary"])
    return assets


def migrate_legacy_reference_assets():
    """Turn every legacy project's single image_path into its first library asset."""
    for project in db.all(
        "SELECT p.id,p.name,p.image_path FROM projects p "
        "WHERE NOT EXISTS (SELECT 1 FROM reference_assets a WHERE a.project_id=p.id)"
    ):
        timestamp = now()
        db.execute(
            "INSERT INTO reference_assets "
            "(id,project_id,file_path,original_filename,asset_type,subject_name,view_tags_json,priority,description,constraints_text,is_primary,created_at,updated_at) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (uuid.uuid4().hex, project["id"], project["image_path"], Path(project["image_path"]).name,
             "main_product", project["name"], json.dumps(["hero", "front"]), "core",
             "旧项目主商品参考图", "保持商品外观、结构、颜色、包装文字和品牌标识一致", 1,
             timestamp, timestamp),
        )


def normalize_storyboard_continuity(board: Storyboard, assets: Optional[list[dict]] = None) -> Storyboard:
    assets = assets or []
    valid_asset_ids = {asset["id"] for asset in assets}
    defaults = [asset["id"] for asset in assets if asset.get("priority") == "core"][:4]
    if not defaults:
        defaults = [asset["id"] for asset in assets[:4]]
    for index, shot in enumerate(board.shots):
        shot.reference_asset_ids = list(dict.fromkeys(
            asset_id for asset_id in shot.reference_asset_ids if asset_id in valid_asset_ids
        ))[:9]
        if shot.continuity_mode == "independent" and not shot.reference_asset_ids:
            shot.reference_asset_ids = defaults
            if assets:
                shot.reference_source = "system"
        if index == 0:
            shot.continuity_mode = "independent"
            shot.transition_type = "cut"
            shot.transition_duration_ms = 0
            if not shot.reference_asset_ids:
                shot.reference_asset_ids = defaults
                if assets:
                    shot.reference_source = "system"
            continue
        previous = board.shots[index - 1]
        if shot.continuity_mode == "carry_last_frame" and (
            not shot.continuity_group or shot.continuity_group != previous.continuity_group
        ):
            shot.continuity_mode = "independent"
        if shot.transition_type == "cut":
            shot.transition_duration_ms = 0
        elif shot.transition_duration_ms == 0:
            shot.transition_duration_ms = {
                "match_cut": 100, "dissolve": 280, "dip_to_black": 350,
            }[shot.transition_type]
    return board


async def evaluate_storyboard(provider, board: Storyboard, points: str,
                              assets: list[dict]) -> QAReport:
    local = deterministic_qa(board, points)
    model_qa = None
    if settings.m3_enable_review:
        try:
            model_qa = await provider.review(board, points, assets)
        except Exception as exc:
            model_qa = QAReport(
                passed=True, score=85,
                issues=[{"severity": "warning", "code": "review_unavailable",
                         "message": provider_error_message(exc, "M3 脚本复核"),
                         "shot_position": None}],
            )
    return merge_qa(local, model_qa, settings.m3_storyboard_pass_score)


def storyboard_rank(item: tuple[Storyboard, QAReport]) -> tuple[int, int, int, int]:
    _, qa = item
    errors = sum(issue.severity == "error" for issue in qa.issues)
    return (int(qa.passed), qa.score, -errors, -len(qa.issues))


async def generate_storyboard(project_id: str):
    p = db.one("SELECT * FROM projects WHERE id=?", (project_id,))
    if not p:
        raise KeyError(project_id)
    try:
        provider = m3_provider()
        assets = reference_assets(project_id)
        board = normalize_storyboard_continuity(
            await provider.storyboard(p["name"], p["selling_points"], assets), assets
        )
        candidates: list[tuple[Storyboard, QAReport]] = []
        max_attempts = max(1, settings.m3_storyboard_max_attempts)
        for attempt in range(max_attempts):
            qa = await evaluate_storyboard(provider, board, p["selling_points"], assets)
            candidates.append((board, qa))
            if qa.passed or attempt == max_attempts - 1:
                break
            try:
                board = normalize_storyboard_continuity(
                    await provider.revise_storyboard(
                        board, p["name"], p["selling_points"], assets, qa
                    ),
                    assets,
                )
            except Exception as exc:
                best_board, best_qa = max(candidates, key=storyboard_rank)
                best_qa.issues.append(QAIssue(
                    severity="warning", code="auto_revision_unavailable",
                    message=provider_error_message(exc, "M3 自动修订"),
                ))
                board, qa = best_board, best_qa
                break
        board, qa = max(candidates + [(board, qa)], key=storyboard_rank)
        if not qa.passed and len(candidates) >= max_attempts:
            qa.issues.append(QAIssue(
                severity="warning", code="auto_revision_limit",
                message=f"已完成 {max_attempts} 轮生成/修订，当前展示其中得分最高的一版；可人工修改后重新评估。",
            ))
        with db.conn() as con:
            current = con.execute("SELECT status FROM projects WHERE id=?", (project_id,)).fetchone()
            if not current or current["status"] != "scripting":
                return
            con.execute("DELETE FROM shots WHERE project_id=?", (project_id,))
            con.execute("UPDATE projects SET script_json=?, qa_json=?, status=?, confirmed_at=NULL, qa_overridden_at=NULL, qa_override_reason=NULL, preview_path=NULL, preview_error=NULL, updated_at=? WHERE id=?",
                        (board.model_dump_json(), qa.model_dump_json(), "awaiting_confirmation" if qa.passed else "qa_failed", now(), project_id))
            for s in board.shots:
                con.execute(
                    "INSERT INTO shots (id,project_id,position,title,duration,prompt,voiceover,overlay_text,status,attempts,provider_task_id,output_url,error,created_at,updated_at) "
                    "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (uuid.uuid4().hex, project_id, s.position, s.title, s.duration, s.prompt,
                     s.voiceover, s.overlay_text, "draft", 0, None, None, None, now(), now()),
                )
    except Exception as exc:
        db.execute("UPDATE projects SET status='script_failed', qa_json=?, updated_at=? WHERE id=? AND status='scripting'",
                   (json.dumps({"passed":False,"score":0,"issues":[{"severity":"error","code":"m3_failure","message":provider_error_message(exc, "M3 分镜生成"),"shot_position":None}]}, ensure_ascii=False), now(), project_id))


async def recheck_storyboard(project_id: str):
    p = db.one("SELECT * FROM projects WHERE id=?", (project_id,))
    if not p or p["confirmed_at"] or p["status"] not in {"awaiting_confirmation", "qa_failed", "qa_stale"}:
        raise ValueError("只有尚未确认的分镜可以修改并重新质检")
    script = json.loads(p["script_json"])
    shot_rows = db.all("SELECT * FROM shots WHERE project_id=? ORDER BY position", (project_id,))
    plans = {item["position"]: item for item in script.get("shots", [])}
    script["shots"] = []
    for shot in shot_rows:
        plan = plans.get(shot["position"], {})
        plan.update({key: shot[key] for key in ("position", "title", "duration", "prompt", "voiceover", "overlay_text")})
        script["shots"].append(plan)
    assets = reference_assets(project_id)
    board = normalize_storyboard_continuity(Storyboard.model_validate(script), assets)
    local = deterministic_qa(board, p["selling_points"])
    model_qa = None
    if settings.m3_enable_review:
        try:
            model_qa = await m3_provider().review(board, p["selling_points"], assets)
        except Exception as exc:
            model_qa = QAReport(
                passed=True,
                score=85,
                issues=[{"severity": "warning", "code": "review_unavailable",
                         "message": provider_error_message(exc, "M3 脚本复核"), "shot_position": None}],
            )
    qa = merge_qa(local, model_qa, settings.m3_storyboard_pass_score)
    db.execute(
        "UPDATE projects SET script_json=?, qa_json=?, status=?, qa_overridden_at=NULL, qa_override_reason=NULL, updated_at=? "
        "WHERE id=? AND confirmed_at IS NULL",
        (board.model_dump_json(), qa.model_dump_json(),
         "awaiting_confirmation" if qa.passed else "qa_failed", now(), project_id),
    )
    return qa


async def cache_video(project_id: str, shot_id: str, video_url: str) -> Optional[str]:
    """Download a provider result before its signed URL expires."""
    if "example.invalid" in video_url:
        return None
    video_dir = settings.data_dir / "videos" / project_id
    video_dir.mkdir(parents=True, exist_ok=True)
    final_path = video_dir / f"{shot_id}.mp4"
    temp_path = video_dir / f".{shot_id}.part"
    total = 0
    try:
        async with httpx.AsyncClient(follow_redirects=True, timeout=300) as client:
            async with client.stream("GET", video_url) as response:
                response.raise_for_status()
                declared = int(response.headers.get("content-length", "0") or 0)
                if declared > settings.max_video_bytes:
                    raise ValueError("视频超过本地缓存大小限制")
                with temp_path.open("wb") as output:
                    async for chunk in response.aiter_bytes():
                        total += len(chunk)
                        if total > settings.max_video_bytes:
                            raise ValueError("视频超过本地缓存大小限制")
                        output.write(chunk)
        temp_path.replace(final_path)
        return str(final_path)
    except Exception:
        temp_path.unlink(missing_ok=True)
        raise


async def review_generated_video(shot: dict, video_url: str, image_path: str) -> Optional[VideoQAReport]:
    if not settings.m3_enable_video_review:
        return None
    try:
        return await m3_provider().review_video(shot, video_url, image_path)
    except Exception as exc:
        return VideoQAReport(
            passed=False,
            score=0,
            issues=[{"severity": "warning", "code": "review_unavailable",
                     "message": provider_error_message(exc, "M3 视频质检")}],
            recommendation="视频已经生成，可人工查看后决定是否采用或重试。",
            retry_prompt="",
        )


def shot_plan(project: dict, position: int) -> dict:
    script = json.loads(project["script_json"]) if project.get("script_json") else {}
    plan = next((item for item in script.get("shots", []) if item.get("position") == position), {})
    if "transition_type" not in plan:
        title = plan.get("title", "")
        if position == 1:
            plan.update(transition_type="cut", transition_duration_ms=0, continuity_mode="independent")
        elif any(word in title for word in ("结尾", "品牌", "收束")):
            plan.update(transition_type="dip_to_black", transition_duration_ms=350, continuity_mode="independent")
        elif any(word in title for word in ("使用", "上脸", "场景")):
            plan.update(transition_type="dissolve", transition_duration_ms=280, continuity_mode="independent")
        else:
            plan.update(transition_type="match_cut", transition_duration_ms=100, continuity_mode="independent")
    return plan


def backfill_legacy_continuity_plans():
    for project in db.all("SELECT id,script_json FROM projects WHERE script_json IS NOT NULL"):
        script = json.loads(project["script_json"])
        assets = reference_assets(project["id"])
        default_asset_ids = [asset["id"] for asset in assets if asset["priority"] == "core"][:4] or [asset["id"] for asset in assets[:4]]
        changed = False
        for item in script.get("shots", []):
            if "transition_type" not in item:
                fallback = shot_plan(project, item["position"])
                item.update({
                    "continuity_mode": fallback["continuity_mode"],
                    "transition_type": fallback["transition_type"],
                    "transition_duration_ms": fallback["transition_duration_ms"],
                    "entry_action": "",
                    "exit_action": "",
                    "continuity_group": None,
                })
                changed = True
            if "continuity_source" not in item:
                item["continuity_source"] = "system"
                changed = True
            reference_ids = item.get("reference_asset_ids")
            if (not isinstance(reference_ids, list) or not reference_ids) and item.get("continuity_mode") != "carry_last_frame" and default_asset_ids:
                item["reference_asset_ids"] = default_asset_ids
                item["reference_source"] = "system"
                changed = True
            elif "reference_source" not in item:
                item["reference_source"] = "system"
                changed = True
        if changed:
            db.execute(
                "UPDATE projects SET script_json=? WHERE id=?",
                (json.dumps(script, ensure_ascii=False), project["id"]),
            )


async def extract_tail_frame(project_id: str, previous_shot: dict) -> str:
    source = previous_shot.get("local_video_path")
    if not source or not Path(source).is_file():
        raise ValueError("上一镜没有可用的本地视频，无法提取连续性尾帧")
    target_dir = settings.data_dir / "keyframes" / project_id
    target_dir.mkdir(parents=True, exist_ok=True)
    target = target_dir / f"{previous_shot['id']}-tail.jpg"
    process = await asyncio.create_subprocess_exec(
        settings.ffmpeg_path, "-y", "-sseof", "-0.20", "-i", source,
        "-frames:v", "1", "-q:v", "2", str(target),
        stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.PIPE,
    )
    _, stderr = await process.communicate()
    if process.returncode or not target.is_file():
        raise RuntimeError(f"提取上一镜尾帧失败：{stderr.decode(errors='replace')[-500:]}")
    return str(target)


async def probe_media(path: str) -> dict:
    process = await asyncio.create_subprocess_exec(
        settings.ffprobe_path, "-v", "error", "-show_entries",
        "format=duration:stream=index,codec_type,width,height", "-of", "json", path,
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
    )
    stdout, stderr = await process.communicate()
    if process.returncode:
        raise RuntimeError(f"读取视频信息失败：{stderr.decode(errors='replace')[-500:]}")
    return json.loads(stdout)


async def build_project_preview(project_id: str) -> Optional[str]:
    project = db.one("SELECT * FROM projects WHERE id=?", (project_id,))
    shots = db.all("SELECT * FROM shots WHERE project_id=? ORDER BY position", (project_id,))
    if not project or not shots or not all(s["status"] == "succeeded" for s in shots):
        return None
    if not all(s.get("local_video_path") and Path(s["local_video_path"]).is_file() for s in shots):
        db.execute("UPDATE projects SET preview_path=NULL, preview_error=? WHERE id=?", ("部分镜头没有本地缓存，暂时无法合成预览", project_id))
        return None
    try:
        metadata = await asyncio.gather(*(probe_media(s["local_video_path"]) for s in shots))
        durations = [float(item["format"]["duration"]) for item in metadata]
        first_video = next(stream for stream in metadata[0]["streams"] if stream["codec_type"] == "video")
        width, height = first_video["width"], first_video["height"]
        all_have_audio = all(any(stream["codec_type"] == "audio" for stream in item["streams"]) for item in metadata)
        filters = []
        for index in range(len(shots)):
            filters.append(
                f"[{index}:v]scale={width}:{height}:force_original_aspect_ratio=decrease,"
                f"pad={width}:{height}:(ow-iw)/2:(oh-ih)/2,setsar=1,fps=24,settb=AVTB[v{index}]"
            )
            if all_have_audio:
                filters.append(f"[{index}:a]aresample=48000,asetpts=PTS-STARTPTS[a{index}]")
        video_label, audio_label, timeline = "v0", "a0", durations[0]
        for index in range(1, len(shots)):
            plan = shot_plan(project, shots[index]["position"])
            transition_type = plan.get("transition_type", "cut")
            requested = float(plan.get("transition_duration_ms") or 0) / 1000
            if transition_type == "cut":
                transition, duration = "fade", 0.04
            elif transition_type == "match_cut":
                transition, duration = "fade", max(0.06, requested or 0.10)
            elif transition_type == "dip_to_black":
                transition, duration = "fadeblack", max(0.20, requested or 0.35)
            else:
                transition, duration = "fade", max(0.15, requested or 0.28)
            duration = min(duration, durations[index - 1] / 2, durations[index] / 2, 1.0)
            offset = max(0, timeline - duration)
            next_video = f"vx{index}"
            filters.append(
                f"[{video_label}][v{index}]xfade=transition={transition}:duration={duration:.3f}:offset={offset:.3f}[{next_video}]"
            )
            video_label = next_video
            if all_have_audio:
                next_audio = f"ax{index}"
                filters.append(f"[{audio_label}][a{index}]acrossfade=d={duration:.3f}:c1=tri:c2=tri[{next_audio}]")
                audio_label = next_audio
            timeline += durations[index] - duration

        preview_dir = settings.data_dir / "previews"
        preview_dir.mkdir(parents=True, exist_ok=True)
        final_path = preview_dir / f"{project_id}.mp4"
        temp_path = preview_dir / f".{project_id}.part.mp4"
        command = [settings.ffmpeg_path, "-y"]
        for shot in shots:
            command.extend(["-i", shot["local_video_path"]])
        command.extend(["-filter_complex", ";".join(filters), "-map", f"[{video_label}]", "-c:v", "libx264", "-crf", "20", "-preset", "fast", "-pix_fmt", "yuv420p"])
        if all_have_audio:
            command.extend(["-map", f"[{audio_label}]", "-c:a", "aac", "-b:a", "192k"])
        else:
            command.append("-an")
        command.extend(["-movflags", "+faststart", str(temp_path)])
        process = await asyncio.create_subprocess_exec(
            *command, stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.PIPE,
        )
        _, stderr = await process.communicate()
        if process.returncode or not temp_path.is_file():
            raise RuntimeError(stderr.decode(errors="replace")[-1000:])
        temp_path.replace(final_path)
        db.execute("UPDATE projects SET preview_path=?, preview_error=NULL, updated_at=? WHERE id=?", (str(final_path), now(), project_id))
        return str(final_path)
    except Exception as exc:
        db.execute("UPDATE projects SET preview_path=NULL, preview_error=? WHERE id=?", (f"合成预览失败：{str(exc)[:800]}", project_id))
        return None


async def process_shot(shot: dict):
    p = db.one("SELECT * FROM projects WHERE id=?", (shot["project_id"],))
    if not p:
        return
    provider = h3_provider()
    if shot["status"] == "queued":
        first_frame_path = None
        plan = shot_plan(p, shot["position"])
        if settings.h3_enable_frame_chaining and plan.get("continuity_mode") == "carry_last_frame" and shot["position"] > 1:
            previous = db.one(
                "SELECT * FROM shots WHERE project_id=? AND position=?",
                (shot["project_id"], shot["position"] - 1),
            )
            if not previous or previous["status"] in {"draft", "queued", "submitting", "submitted", "running"}:
                return
            if previous["status"] == "failed":
                db.execute(
                    "UPDATE shots SET status='failed', error='连续性依赖的上一镜生成失败；请先重试上一镜后再续跑', updated_at=? WHERE id=? AND status='queued'",
                    (now(), shot["id"]),
                )
                return
            try:
                first_frame_path = await extract_tail_frame(shot["project_id"], previous)
            except Exception as exc:
                db.execute(
                    "UPDATE shots SET status='failed', error=?, updated_at=? WHERE id=? AND status='queued'",
                    (str(exc)[:800], now(), shot["id"]),
                )
                return
        with db.conn() as con:
            claimed = con.execute(
                "UPDATE shots SET status='submitting', attempts=attempts+1, error=NULL, updated_at=? "
                "WHERE id=? AND status='queued' AND attempts < ?",
                (now(), shot["id"], settings.max_shot_attempts),
            ).rowcount
        if not claimed:
            db.execute("UPDATE shots SET status='failed', error='超过最大尝试次数', updated_at=? WHERE id=? AND status='queued'", (now(), shot["id"]))
            return
        try:
            submission_shot = dict(shot)
            assets = reference_assets(shot["project_id"])
            assets_by_id = {asset["id"]: asset for asset in assets}
            selected_assets = [assets_by_id[asset_id] for asset_id in plan.get("reference_asset_ids", []) if asset_id in assets_by_id]
            if not selected_assets:
                selected_assets = [asset for asset in assets if asset["priority"] == "core"][:4] or assets[:4]
            primary_asset = next((asset for asset in assets if asset["is_primary"]), None) or (assets[0] if assets else None)
            if selected_assets:
                manifest = []
                chinese_prompt = prompt_uses_chinese(submission_shot["prompt"])
                for index, asset in enumerate(selected_assets, 1):
                    raw_parts = ["、".join(asset["view_tags"]), asset["description"]]
                    raw_parts = [part for part in raw_parts if part and same_prompt_language(part, chinese_prompt)]
                    if asset["constraints"] and same_prompt_language(asset["constraints"], chinese_prompt):
                        raw_parts.append(("必须保持：" if chinese_prompt else "Must preserve: ") + asset["constraints"])
                    details = ("；" if chinese_prompt else "; ").join(raw_parts)
                    if details:
                        subject = asset["subject_name"] if same_prompt_language(asset["subject_name"], chinese_prompt) else asset["asset_type"]
                        manifest.append((f"参考图{index}（{subject}）：{details}" if chinese_prompt else f"Reference image {index} ({subject}): {details}"))
                if manifest:
                    heading, separator = (" 参考素材说明：", "；") if chinese_prompt else (" Reference materials: ", "; ")
                    submission_shot["prompt"] = (submission_shot["prompt"] + heading + separator.join(manifest))[:1500]
            if first_frame_path and "从提供的首帧自然延续" not in submission_shot["prompt"] and "supplied first frame" not in submission_shot["prompt"]:
                if prompt_uses_chinese(submission_shot["prompt"]):
                    requirement = " 连续性要求：从提供的首帧自然延续，保持主体、场景、光线和运动方向连贯，避免开场跳变。"
                else:
                    requirement = " Continuity requirement: continue naturally from the supplied first frame; preserve the subject, setting, lighting, and direction of motion without an opening jump."
                submission_shot["prompt"] = (submission_shot["prompt"] + requirement)[:1500]
            if first_frame_path:
                task_id = await provider.submit(
                    submission_shot, p["image_path"], json.loads(p["script_json"])["aspect_ratio"],
                    first_frame_path=first_frame_path,
                )
            else:
                reference_paths = [asset["file_path"] for asset in selected_assets[:9]]
                image_path = reference_paths[0] if reference_paths else (primary_asset["file_path"] if primary_asset else p["image_path"])
                if len(reference_paths) > 1:
                    task_id = await provider.submit(
                        submission_shot, image_path, json.loads(p["script_json"])["aspect_ratio"],
                        reference_image_paths=reference_paths,
                    )
                else:
                    task_id = await provider.submit(submission_shot, image_path, json.loads(p["script_json"])["aspect_ratio"])
            db.execute("UPDATE shots SET status='submitted', provider_task_id=?, error=NULL, updated_at=? WHERE id=? AND status='submitting'", (task_id, now(), shot["id"]))
        except Exception as exc:
            db.execute("UPDATE shots SET status='failed', error=?, updated_at=? WHERE id=? AND status='submitting'", (str(exc), now(), shot["id"]))
        return

    try:
        result = await provider.poll(shot["provider_task_id"])
        if result["status"] == "succeeded":
            if not result.get("output_url"):
                raise ValueError("H3 成功响应缺少视频 URL")
            output_url = result["output_url"]
            cached, video_qa = await asyncio.gather(
                cache_video(shot["project_id"], shot["id"], output_url),
                review_generated_video(shot, output_url, p["image_path"]),
                return_exceptions=True,
            )
            cache_error = cached if isinstance(cached, Exception) else None
            local_path = None if cache_error else cached
            if isinstance(video_qa, Exception):
                video_qa = VideoQAReport(
                    passed=False, score=0,
                    issues=[{"severity": "warning", "code": "review_unavailable", "message": "M3 视频质检暂不可用"}],
                    recommendation="视频已经生成，可人工查看后决定是否采用或重试。",
                )
            error = f"视频本地缓存失败：{str(cache_error)[:400]}" if cache_error else None
            db.execute(
                "UPDATE shots SET status='succeeded', output_url=?, local_video_path=?, video_qa_json=?, error=?, updated_at=? WHERE id=?",
                (output_url, local_path, video_qa.model_dump_json() if video_qa else None, error, now(), shot["id"]),
            )
        elif result["status"] == "failed":
            db.execute("UPDATE shots SET status='failed', error=?, updated_at=? WHERE id=?", (result.get("error", "H3 生成失败"), now(), shot["id"]))
        else:
            db.execute("UPDATE shots SET status='running', error=NULL, updated_at=? WHERE id=?", (now(), shot["id"]))
    except Exception as exc:
        # A polling transport error does not mean the remote generation failed.
        # Keep the task id and poll again to avoid duplicate H3 submissions.
        db.execute("UPDATE shots SET error=?, updated_at=? WHERE id=? AND status IN ('submitted','running')", (f"H3 状态查询暂时失败：{exc}", now(), shot["id"]))


async def worker(stop: asyncio.Event):
    while not stop.is_set():
        shots = db.all("SELECT * FROM shots WHERE status IN ('queued','submitted','running') ORDER BY updated_at LIMIT 10")
        if shots:
            await asyncio.gather(*(process_shot(s) for s in shots))
            project_ids = {s["project_id"] for s in shots}
            for pid in project_ids:
                states = [r["status"] for r in db.all("SELECT status FROM shots WHERE project_id=?", (pid,))]
                if states and all(s == "succeeded" for s in states):
                    status = "completed"
                elif any(s in {"queued", "submitted", "running"} for s in states):
                    status = "generating"
                elif any(s == "failed" for s in states):
                    status = "partial_failed"
                elif any(s == "revision_pending" for s in states):
                    status = "revision_pending"
                else:
                    continue
                db.execute("UPDATE projects SET status=?, updated_at=? WHERE id=?", (status, now(), pid))
                if status == "completed":
                    await build_project_preview(pid)
        try:
            await asyncio.wait_for(stop.wait(), timeout=settings.worker_poll_seconds)
        except asyncio.TimeoutError:
            pass
