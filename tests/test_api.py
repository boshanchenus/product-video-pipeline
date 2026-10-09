import asyncio
import time
import json
import shutil
import subprocess

import httpx
import pytest
from fastapi.testclient import TestClient

from app import service
from app.config import settings
from app.db import db
from app.main import app
from app.models import QAReport, ShotRewriteResult, Storyboard
from app.providers import HttpH3Provider, OpenAIM3Provider, extract_json, h3_prompt_with_audio_direction


PNG = b"\x89PNG\r\n\x1a\n" + b"test-image"


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "data_dir", tmp_path)
    monkeypatch.setattr(settings, "m3_mode", "mock")
    monkeypatch.setattr(settings, "h3_mode", "mock")
    monkeypatch.setattr(settings, "worker_poll_seconds", 0.01)
    monkeypatch.setattr(settings, "max_shot_attempts", 3)
    monkeypatch.setattr(settings, "max_upload_bytes", 1024 * 1024)
    db.path = tmp_path / "pipeline.db"
    with TestClient(app) as test_client:
        yield test_client


def create_project(client, *, name="补水精华", points="补水精华"):
    response = client.post(
        "/api/projects",
        data={"name": name, "selling_points": points},
        files={"image": ("product.png", PNG, "image/png")},
    )
    assert response.status_code == 202, response.text
    return response.json()["id"]


def wait_for_status(client, project_id, expected, timeout=2):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        project = client.get(f"/api/projects/{project_id}").json()
        if project["status"] == expected:
            return project
        time.sleep(0.02)
    pytest.fail(f"project did not reach {expected}: {project}")


def test_complete_mock_pipeline_and_state_guards(client):
    project_id = create_project(client)
    project = client.get(f"/api/projects/{project_id}").json()
    assert project["status"] == "awaiting_confirmation"
    assert project["qa"]["passed"] is True
    assert len(project["shots"]) >= 3

    assert client.post(f"/api/projects/{project_id}/confirm").status_code == 200
    assert client.post(f"/api/projects/{project_id}/regenerate-script").status_code == 409

    completed = wait_for_status(client, project_id, "completed")
    assert all(shot["status"] == "succeeded" for shot in completed["shots"])
    assert all(shot["attempts"] == 1 for shot in completed["shots"])
    assert all(shot["video_qa"]["score"] == 91 for shot in completed["shots"])
    assert client.post(f"/api/projects/{project_id}/resume").status_code == 409
    assert client.post(
        f"/api/projects/{project_id}/shots/{completed['shots'][0]['id']}/retry"
    ).status_code == 200
    completed_again = wait_for_status(client, project_id, "completed")
    retried = next(shot for shot in completed_again["shots"] if shot["id"] == completed["shots"][0]["id"])
    assert retried["attempts"] == 2


def test_export_is_unavailable_until_composite_video_exists(client):
    project_id = create_project(client)
    stale_preview = settings.data_dir / "previews" / f"{project_id}.mp4"
    stale_preview.parent.mkdir(parents=True, exist_ok=True)
    stale_preview.write_bytes(b"stale-video-from-an-earlier-run")
    db.execute("UPDATE projects SET preview_path=? WHERE id=?", (str(stale_preview), project_id))

    project = client.get(f"/api/projects/{project_id}").json()
    assert project["status"] == "awaiting_confirmation"
    assert project["preview_ready"] is False
    assert "preview_url" not in project
    assert "export_url" not in project
    preview = client.get(f"/api/projects/{project_id}/preview")
    assert preview.status_code == 404
    response = client.get(f"/api/projects/{project_id}/export")
    assert response.status_code == 404
    assert response.json()["detail"] == "完整成片尚未生成"


def test_completed_shots_support_full_plan_edits_and_explicit_regeneration(client):
    project_id = create_project(client, name="成片后完整编辑")
    assert client.post(f"/api/projects/{project_id}/confirm").status_code == 200
    completed = wait_for_status(client, project_id, "completed")
    first, second = completed["shots"][:2]

    preview_dir = settings.data_dir / "previews"
    preview_dir.mkdir(parents=True, exist_ok=True)
    preview_path = preview_dir / f"{project_id}.mp4"
    preview_path.write_bytes(b"old-composite")
    db.execute("UPDATE projects SET preview_path=? WHERE id=?", (str(preview_path), project_id))

    def edit(shot, title, transition):
        response = client.patch(
            f"/api/projects/{project_id}/shots/{shot['id']}",
            json={
                "title": title,
                "duration": 6,
                "prompt": f"{title}，保持商品包装、文字和颜色一致，镜头缓慢移动。",
                "voiceover": f"{title}的新旁白",
                "overlay_text": title,
                "transition_type": transition,
                "transition_duration_ms": 240 if transition != "cut" else 0,
                "entry_action": "从稳定构图自然开始",
                "exit_action": "动作结束后稳定停留",
                "continuity_group": "revised-sequence",
            },
        )
        assert response.status_code == 200, response.text
        assert response.json()["requires_regeneration"] is True

    edit(first, "修改后的第一镜", "cut")
    edit(second, "修改后的第二镜", "dissolve")
    pending = client.get(f"/api/projects/{project_id}").json()
    assert pending["status"] == "revision_pending"
    assert pending["preview_ready"] is False
    assert "export_url" not in pending
    assert [shot["status"] for shot in pending["shots"][:2]] == ["revision_pending", "revision_pending"]
    assert pending["shots"][1]["transition_type"] == "dissolve"
    assert pending["script"]["shots"][1]["entry_action"] == "从稳定构图自然开始"

    retry_first = client.post(
        f"/api/projects/{project_id}/shots/{first['id']}/retry",
        json={"prompt": pending["shots"][0]["prompt"]},
    )
    assert retry_first.status_code == 200, retry_first.text
    still_pending = wait_for_status(client, project_id, "revision_pending")
    assert next(shot for shot in still_pending["shots"] if shot["id"] == second["id"])["status"] == "revision_pending"

    retry_second = client.post(
        f"/api/projects/{project_id}/shots/{second['id']}/retry",
        json={"prompt": pending["shots"][1]["prompt"]},
    )
    assert retry_second.status_code == 200, retry_second.text
    regenerated = wait_for_status(client, project_id, "completed")
    assert [shot["title"] for shot in regenerated["shots"][:2]] == ["修改后的第一镜", "修改后的第二镜"]
    assert [shot["attempts"] for shot in regenerated["shots"][:2]] == [2, 2]


def test_project_list_edit_and_local_video_reopen(client):
    project_id = create_project(client, name="可继续编辑的项目")
    project = client.get(f"/api/projects/{project_id}").json()
    shot = project["shots"][0]

    listing = client.get("/api/projects")
    assert listing.status_code == 200
    summary = next(item for item in listing.json() if item["id"] == project_id)
    assert summary["name"] == "可继续编辑的项目"
    assert summary["shot_count"] == len(project["shots"])

    edited = client.patch(
        f"/api/projects/{project_id}/shots/{shot['id']}",
        json={
            "title": "人工修改标题",
            "duration": 6,
            "prompt": "保持商品包装、品牌标志和颜色完全一致，缓慢推进展示产品细节。",
            "voiceover": "这是人工修改后的旁白",
            "overlay_text": "人工已修改",
            "transition_type": "cut",
            "transition_duration_ms": 0,
            "entry_action": "从产品正面稳定构图开始",
            "exit_action": "缓慢推进后稳定停留",
            "continuity_group": "product-hero",
        },
    )
    assert edited.status_code == 200, edited.text
    reopened = client.get(f"/api/projects/{project_id}").json()
    edited_shot = next(item for item in reopened["shots"] if item["id"] == shot["id"])
    assert edited_shot["title"] == "人工修改标题"
    assert edited_shot["entry_action"] == "从产品正面稳定构图开始"
    assert edited_shot["exit_action"] == "缓慢推进后稳定停留"
    assert edited_shot["continuity_group"] == "product-hero"
    assert reopened["script"]["shots"][0]["title"] == "人工修改标题"
    assert reopened["qa"] == project["qa"]
    assert reopened["status"] == "qa_stale"
    assert reopened["confirmed_at"] is None

    video_path = settings.data_dir / "videos" / project_id / f"{shot['id']}.mp4"
    video_path.parent.mkdir(parents=True)
    video_path.write_bytes(b"fake-mp4-content")
    db.execute("UPDATE shots SET local_video_path=? WHERE id=?", (str(video_path), shot["id"]))
    reopened = client.get(f"/api/projects/{project_id}").json()
    edited_shot = next(item for item in reopened["shots"] if item["id"] == shot["id"])
    assert edited_shot["video_cached"] is True
    response = client.get(edited_shot["local_video_url"])
    assert response.status_code == 200
    assert response.content == b"fake-mp4-content"


def test_multiple_shot_edits_are_saved_before_one_manual_recheck(client, monkeypatch):
    project_id = create_project(client, name="批量修改后统一评估")
    project = client.get(f"/api/projects/{project_id}").json()
    first, second = project["shots"][:2]
    provider = service.m3_provider()
    review_calls = 0
    original_review = provider.review

    async def counting_review(board, points, assets):
        nonlocal review_calls
        review_calls += 1
        return await original_review(board, points, assets)

    monkeypatch.setattr(provider, "review", counting_review)
    monkeypatch.setattr(service, "m3_provider", lambda: provider)

    for shot, title in ((first, "第一镜批量修改"), (second, "第二镜批量修改")):
        response = client.patch(
            f"/api/projects/{project_id}/shots/{shot['id']}",
            json={
                "title": title,
                "duration": shot["duration"],
                "prompt": shot["prompt"] + " 保持画面稳定。",
                "voiceover": shot["voiceover"],
                "overlay_text": shot["overlay_text"],
            },
        )
        assert response.status_code == 200, response.text
        assert response.json()["qa_stale"] is True

    assert review_calls == 0
    pending = client.get(f"/api/projects/{project_id}").json()
    assert pending["qa"] == project["qa"]
    assert pending["status"] == "qa_stale"
    assert client.post(f"/api/projects/{project_id}/confirm").status_code == 409

    checked = client.post(f"/api/projects/{project_id}/recheck-script")
    assert checked.status_code == 200, checked.text
    assert review_calls == 1
    reopened = client.get(f"/api/projects/{project_id}").json()
    assert reopened["qa"] is not None
    assert [shot["title"] for shot in reopened["shots"][:2]] == ["第一镜批量修改", "第二镜批量修改"]


def test_m3_rewrites_only_one_draft_shot_with_full_storyboard_context(client, monkeypatch):
    project_id = create_project(client, name="单镜重写测试", points="轻盈补水，清爽不黏腻")
    before = client.get(f"/api/projects/{project_id}").json()
    target = before["shots"][1]
    original_plans = {item["position"]: item for item in before["script"]["shots"]}
    captured = {}

    class ContextAwareRewriter:
        async def rewrite_shot(self, board, position, name, points, assets, qa, instruction):
            captured.update({
                "positions": [shot.position for shot in board.shots],
                "position": position,
                "name": name,
                "points": points,
                "asset_count": len(assets),
                "qa_score": qa.score if qa else None,
                "instruction": instruction,
            })
            return ShotRewriteResult(
                title="质地延展特写",
                prompt="微距展示透明精华在手背轻柔延展，保持参考包装文字、瓶身结构和液体颜色一致。",
                voiceover="轻盈水感，触肤清爽。",
                overlay_text="轻盈水感",
                entry_action="承接上一镜持瓶动作切入手背特写",
                exit_action="精华完全延展后稳定停留",
            )

    monkeypatch.setattr(service, "m3_provider", lambda: ContextAwareRewriter())
    response = client.post(
        f"/api/projects/{project_id}/shots/{target['id']}/rewrite-script",
        json={"instruction": "增加质地延展的视觉证据，减少抽象光影"},
    )
    assert response.status_code == 200, response.text
    assert response.json()["status"] == "qa_stale"

    after = client.get(f"/api/projects/{project_id}").json()
    assert after["status"] == "qa_stale"
    assert after["qa"] == before["qa"]
    assert captured["positions"] == [shot["position"] for shot in before["shots"]]
    assert captured["position"] == target["position"]
    assert captured["name"] == "单镜重写测试"
    assert captured["points"] == "轻盈补水，清爽不黏腻"
    assert captured["asset_count"] == len(before["reference_assets"])
    assert captured["qa_score"] == before["qa"]["score"]
    assert captured["instruction"] == "增加质地延展的视觉证据，减少抽象光影"
    rewritten_shot = next(shot for shot in after["shots"] if shot["id"] == target["id"])
    assert rewritten_shot["status"] == "draft"
    assert rewritten_shot["attempts"] == 0
    assert after["confirmed_at"] is None

    updated_plans = {item["position"]: item for item in after["script"]["shots"]}
    assert updated_plans[target["position"]]["title"] == "质地延展特写"
    assert updated_plans[target["position"]]["duration"] == original_plans[target["position"]]["duration"]
    assert updated_plans[target["position"]]["transition_type"] == original_plans[target["position"]]["transition_type"]
    assert updated_plans[target["position"]]["continuity_mode"] == original_plans[target["position"]]["continuity_mode"]
    assert updated_plans[target["position"]]["reference_asset_ids"] == original_plans[target["position"]]["reference_asset_ids"]
    for position in original_plans:
        if position != target["position"]:
            assert updated_plans[position] == original_plans[position]


def test_tail_frame_toggle_persists_before_and_after_generation(client):
    project_id = create_project(client, name="尾帧开关测试")
    project = client.get(f"/api/projects/{project_id}").json()
    first, second = project["shots"][:2]
    assert second["continuity_source"] == "model"

    rejected = client.patch(
        f"/api/projects/{project_id}/shots/{first['id']}/continuity",
        json={"enabled": True},
    )
    assert rejected.status_code == 409

    enabled = client.patch(
        f"/api/projects/{project_id}/shots/{second['id']}/continuity",
        json={"enabled": True},
    )
    assert enabled.status_code == 200, enabled.text
    assert enabled.json()["applies_to"] == "confirmation"
    reopened = client.get(f"/api/projects/{project_id}").json()
    second_plan = next(item for item in reopened["script"]["shots"] if item["position"] == 2)
    assert second_plan["continuity_mode"] == "carry_last_frame"
    assert second_plan["continuity_source"] == "human"
    assert second_plan["continuity_group"] == reopened["script"]["shots"][0]["continuity_group"]

    # Turn it back off so the mock pipeline does not require a locally cached
    # predecessor, then verify completed projects keep the same control.
    disabled = client.patch(
        f"/api/projects/{project_id}/shots/{second['id']}/continuity",
        json={"enabled": False},
    )
    assert disabled.status_code == 200
    assert client.post(f"/api/projects/{project_id}/confirm").status_code == 200
    completed = wait_for_status(client, project_id, "completed")
    attempts_before = next(item for item in completed["shots"] if item["id"] == second["id"])["attempts"]

    future = client.patch(
        f"/api/projects/{project_id}/shots/{second['id']}/continuity",
        json={"enabled": True},
    )
    assert future.status_code == 200, future.text
    assert future.json()["applies_to"] == "next_generation"
    reopened = client.get(f"/api/projects/{project_id}").json()
    updated = next(item for item in reopened["shots"] if item["id"] == second["id"])
    assert updated["continuity_mode"] == "carry_last_frame"
    assert updated["continuity_source"] == "human"
    assert updated["attempts"] == attempts_before
    assert reopened["status"] == "completed"


def test_reference_library_crud_and_per_shot_selection(client):
    metadata = [
        {"asset_type": "main_product", "subject_name": "扫地机器人", "view_tags": ["front", "hero"],
         "priority": "core", "description": "白色圆形主机正面", "constraints": "保留黑色雷达塔", "is_primary": True},
        {"asset_type": "accessory", "subject_name": "充电基站", "view_tags": ["front"],
         "priority": "supporting", "description": "自动集尘充电基站", "constraints": "保持黑白配色", "is_primary": False},
    ]
    response = client.post(
        "/api/projects",
        data={"name": "扫地机器人", "selling_points": "自动回充", "asset_metadata": json.dumps(metadata, ensure_ascii=False)},
        files=[("image", ("robot.png", PNG, "image/png")), ("image", ("dock.png", PNG, "image/png"))],
    )
    assert response.status_code == 202, response.text
    project_id = response.json()["id"]
    project = client.get(f"/api/projects/{project_id}").json()
    assert len(project["reference_assets"]) == 2
    primary = next(asset for asset in project["reference_assets"] if asset["is_primary"])
    dock = next(asset for asset in project["reference_assets"] if not asset["is_primary"])
    assert primary["description"] == "白色圆形主机正面"
    assert client.get(primary["url"]).content == PNG

    edited = client.patch(
        f"/api/projects/{project_id}/references/{dock['id']}",
        json={"asset_type": "accessory", "subject_name": "集尘充电基站", "view_tags": ["front", "detail"],
              "priority": "core", "description": "基站正面与集尘口", "constraints": "保持黑白配色", "is_primary": False},
    )
    assert edited.status_code == 200, edited.text
    shot = project["shots"][0]
    selected = client.patch(
        f"/api/projects/{project_id}/shots/{shot['id']}/references",
        json={"asset_ids": [primary["id"], dock["id"]]},
    )
    assert selected.status_code == 200, selected.text
    reopened = client.get(f"/api/projects/{project_id}").json()
    updated_shot = next(item for item in reopened["shots"] if item["id"] == shot["id"])
    assert updated_shot["reference_asset_ids"] == [primary["id"], dock["id"]]
    assert updated_shot["reference_source"] == "human"

    promoted = client.patch(
        f"/api/projects/{project_id}/references/{dock['id']}",
        json={"asset_type": "accessory", "subject_name": "集尘充电基站", "view_tags": ["front"],
              "priority": "core", "description": "新的主参考", "constraints": "保持黑白配色", "is_primary": True},
    )
    assert promoted.status_code == 200
    deleted = client.delete(f"/api/projects/{project_id}/references/{dock['id']}")
    assert deleted.status_code == 200, deleted.text
    reopened = client.get(f"/api/projects/{project_id}").json()
    assert len(reopened["reference_assets"]) == 1
    assert reopened["reference_assets"][0]["id"] == primary["id"]
    assert reopened["reference_assets"][0]["is_primary"] is True
    assert dock["id"] not in reopened["shots"][0]["reference_asset_ids"]
    assert client.delete(f"/api/projects/{project_id}/references/{primary['id']}").status_code == 409

    assert client.post(f"/api/projects/{project_id}/confirm").status_code == 200
    wait_for_status(client, project_id, "completed")
    completed_edit = client.patch(
        f"/api/projects/{project_id}/references/{primary['id']}",
        json={"asset_type": "main_product", "subject_name": "扫地机器人", "view_tags": ["front"],
              "priority": "core", "description": "成片后补充的描述", "constraints": "保留黑色雷达塔", "is_primary": True},
    )
    assert completed_edit.status_code == 200, completed_edit.text
    reopened = client.get(f"/api/projects/{project_id}").json()
    assert reopened["reference_assets"][0]["description"] == "成片后补充的描述"
    assert reopened["status"] == "completed"


def test_legacy_empty_reference_selection_is_backfilled(client):
    project_id = create_project(client, name="旧项目素材迁移")
    row = db.one("SELECT script_json FROM projects WHERE id=?", (project_id,))
    script = json.loads(row["script_json"])
    for plan in script["shots"]:
        plan["reference_asset_ids"] = []
        plan["reference_source"] = "system"
    db.execute("UPDATE projects SET script_json=? WHERE id=?", (json.dumps(script, ensure_ascii=False), project_id))

    service.backfill_legacy_continuity_plans()
    reopened = client.get(f"/api/projects/{project_id}").json()
    primary_id = next(asset["id"] for asset in reopened["reference_assets"] if asset["is_primary"])
    for shot in reopened["shots"]:
        if shot["continuity_mode"] == "independent":
            assert shot["reference_asset_ids"] == [primary_id]
            assert shot["reference_source"] == "system"


def test_delete_project_removes_database_and_owned_files(client):
    project_id = create_project(client, name="待删除项目")
    project_row = db.one("SELECT image_path FROM projects WHERE id=?", (project_id,))
    image_path = settings.data_dir / "uploads" / f"{project_id}.png"
    assert str(image_path) == project_row["image_path"]
    assert image_path.is_file()

    video_dir = settings.data_dir / "videos" / project_id
    video_dir.mkdir(parents=True)
    (video_dir / "cached.mp4").write_bytes(b"video")
    other_id = create_project(client, name="保留项目")
    other_image = settings.data_dir / "uploads" / f"{other_id}.png"

    deleted = client.delete(f"/api/projects/{project_id}")
    assert deleted.status_code == 200
    assert deleted.json() == {"deleted": True, "id": project_id}
    assert client.get(f"/api/projects/{project_id}").status_code == 404
    assert not image_path.exists()
    assert not video_dir.exists()
    assert other_image.is_file()
    assert client.get(f"/api/projects/{other_id}").status_code == 200
    assert client.delete(f"/api/projects/{project_id}").status_code == 404


def test_ffmpeg_builds_single_transitioned_project_preview(client):
    if not shutil.which(settings.ffmpeg_path) or not shutil.which(settings.ffprobe_path):
        pytest.skip("ffmpeg/ffprobe unavailable")
    project_id = create_project(client, name="转场合成测试")
    project = client.get(f"/api/projects/{project_id}").json()
    colors = ["#264d3a", "#b99666", "#d8ded9", "#6f8f7b", "#26332b"]
    for index, shot in enumerate(project["shots"]):
        video_dir = settings.data_dir / "videos" / project_id
        video_dir.mkdir(parents=True, exist_ok=True)
        path = video_dir / f"{shot['id']}.mp4"
        subprocess.run(
            [settings.ffmpeg_path, "-y", "-f", "lavfi", "-i",
             f"color=c={colors[index]}:s=320x568:d=0.8:r=24", "-f", "lavfi", "-i",
             "sine=frequency=440:duration=0.8", "-shortest", "-c:v", "libx264",
             "-pix_fmt", "yuv420p", "-c:a", "aac", str(path)],
            check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )
        db.execute(
            "UPDATE shots SET status='succeeded',local_video_path=? WHERE id=?",
            (str(path), shot["id"]),
        )
    db.execute(
        "UPDATE projects SET status='completed',confirmed_at='test-confirmed' WHERE id=?",
        (project_id,),
    )

    preview_path = asyncio.run(service.build_project_preview(project_id))
    assert preview_path
    reopened = client.get(f"/api/projects/{project_id}").json()
    assert reopened["preview_ready"] is True
    assert reopened["preview_url"].endswith("/preview")
    assert reopened["export_url"].endswith("/export")
    preview = client.get(reopened["preview_url"])
    assert preview.status_code == 200
    assert len(preview.content) > 1000
    exported = client.get(reopened["export_url"])
    assert exported.status_code == 200
    assert exported.content == preview.content
    assert "attachment" in exported.headers["content-disposition"]
    assert ".mp4" in exported.headers["content-disposition"]


def test_qa_failure_can_be_human_overridden_without_starting_h3(client):
    project_id = create_project(client, points="最好，补水精华")
    project = client.get(f"/api/projects/{project_id}").json()
    assert project["status"] == "qa_failed"
    assert project["qa"]["passed"] is False
    assert client.post(f"/api/projects/{project_id}/confirm").status_code == 409

    rejected = client.post(
        f"/api/projects/{project_id}/override-qa",
        json={"acknowledged": False, "reason": "人工判断可以采用"},
    )
    assert rejected.status_code == 422

    overridden = client.post(
        f"/api/projects/{project_id}/override-qa",
        json={"acknowledged": True, "reason": "已人工检查并接受当前风险"},
    )
    assert overridden.status_code == 200
    project = client.get(f"/api/projects/{project_id}").json()
    assert project["status"] == "awaiting_confirmation"
    assert project["qa"]["passed"] is False
    assert project["qa_overridden_at"]
    assert project["qa_override_reason"] == "已人工检查并接受当前风险"
    assert all(shot["status"] == "draft" for shot in project["shots"])

    assert client.post(f"/api/projects/{project_id}/confirm").status_code == 200
    completed = wait_for_status(client, project_id, "completed")
    assert all(shot["status"] == "succeeded" for shot in completed["shots"])


def test_regenerate_before_confirmation_replaces_draft(client):
    project_id = create_project(client)
    before = client.get(f"/api/projects/{project_id}").json()
    before_ids = {shot["id"] for shot in before["shots"]}

    response = client.post(f"/api/projects/{project_id}/regenerate-script")
    assert response.status_code == 202
    after = client.get(f"/api/projects/{project_id}").json()
    assert after["status"] == "awaiting_confirmation"
    assert after["confirmed_at"] is None
    assert {shot["id"] for shot in after["shots"]}.isdisjoint(before_ids)


def test_regenerate_timeout_records_readable_error_and_preserves_previous_draft(client, monkeypatch):
    project_id = create_project(client)
    before = client.get(f"/api/projects/{project_id}").json()
    before_ids = {shot["id"] for shot in before["shots"]}

    class TimeoutM3:
        async def storyboard(self, name, points, assets):
            raise httpx.ReadTimeout("")

    monkeypatch.setattr(service, "m3_provider", lambda: TimeoutM3())
    response = client.post(f"/api/projects/{project_id}/regenerate-script")
    assert response.status_code == 202

    failed = client.get(f"/api/projects/{project_id}").json()
    assert failed["status"] == "script_failed"
    assert {shot["id"] for shot in failed["shots"]} == before_ids
    message = failed["qa"]["issues"][0]["message"]
    assert "请求超时" in message
    assert "ReadTimeout" in message
    assert message.strip()


def test_storyboard_auto_revision_stops_when_second_candidate_passes(client, monkeypatch):
    project_id = create_project(client, name="内部项目名", points="轻盈补水")
    calls = {"review": 0, "revise": 0}

    def board(version):
        return Storyboard(title=f"版本{version}", style="干净商品广告", shots=[
            {"position": i, "title": f"镜头{i}", "duration": 5,
             "prompt": f"轻盈补水商品镜头{i}，保持参考包装文字一致。"}
            for i in range(1, 4)
        ])

    class RevisingM3:
        async def storyboard(self, name, points, assets):
            return board(1)

        async def review(self, candidate, points, assets):
            calls["review"] += 1
            if candidate.title == "版本1":
                return QAReport(passed=False, score=55, issues=[{
                    "severity": "error", "code": "packaging", "message": "包装文字被改写",
                }])
            # Deliberately inconsistent boolean: the central gate must use
            # score + hard errors, rather than trusting this value.
            return QAReport(passed=False, score=82, issues=[{
                "severity": "warning", "code": "polish", "message": "可继续优化布光",
            }])

        async def revise_storyboard(self, candidate, name, points, assets, qa):
            calls["revise"] += 1
            return board(2)

    monkeypatch.setattr(service, "m3_provider", lambda: RevisingM3())
    db.execute("UPDATE projects SET status='scripting' WHERE id=?", (project_id,))
    asyncio.run(service.generate_storyboard(project_id))

    project = client.get(f"/api/projects/{project_id}").json()
    assert project["status"] == "awaiting_confirmation"
    assert project["script"]["title"] == "版本2"
    assert project["qa"]["score"] == 82
    assert calls == {"review": 2, "revise": 1}


def test_storyboard_auto_revision_keeps_best_of_three(client, monkeypatch):
    project_id = create_project(client, points="轻盈补水")
    scores = {"版本1": 50, "版本2": 65, "版本3": 58}
    calls = {"review": 0, "revise": 0}

    def board(version):
        return Storyboard(title=f"版本{version}", style="干净商品广告", shots=[
            {"position": i, "title": f"镜头{i}", "duration": 5,
             "prompt": f"轻盈补水商品镜头{i}，保持参考包装文字一致。"}
            for i in range(1, 4)
        ])

    class ThreeCandidatesM3:
        async def storyboard(self, name, points, assets):
            return board(1)

        async def review(self, candidate, points, assets):
            calls["review"] += 1
            return QAReport(passed=False, score=scores[candidate.title], issues=[{
                "severity": "warning", "code": "needs_work", "message": "仍需人工优化",
            }])

        async def revise_storyboard(self, candidate, name, points, assets, qa):
            calls["revise"] += 1
            return board(calls["revise"] + 1)

    monkeypatch.setattr(service, "m3_provider", lambda: ThreeCandidatesM3())
    monkeypatch.setattr(settings, "m3_storyboard_max_attempts", 3)
    db.execute("UPDATE projects SET status='scripting' WHERE id=?", (project_id,))
    asyncio.run(service.generate_storyboard(project_id))

    project = client.get(f"/api/projects/{project_id}").json()
    assert project["status"] == "qa_failed"
    assert project["script"]["title"] == "版本2"
    assert project["qa"]["score"] == 65
    assert any(i["code"] == "auto_revision_limit" for i in project["qa"]["issues"])
    assert calls == {"review": 3, "revise": 2}


def test_h3_native_voiceover_prompt_disables_per_shot_music(client):
    prompt = h3_prompt_with_audio_direction({
        "prompt": "商品特写，镜头缓慢推进。",
        "voiceover": "轻盈水感，清爽不黏腻。",
    })
    assert "逐字朗读：‘轻盈水感，清爽不黏腻。’" in prompt
    assert "不要求画面人物对口型" in prompt
    assert "不要生成任何背景音乐或旋律" in prompt


def test_extract_json_accepts_model_text_and_duplicate_trailing_content():
    first = {"title": "有效分镜", "shots": [{"position": 1}]}
    response = (
        "```json\n" + json.dumps(first, ensure_ascii=False) + "\n```\n"
        "以下是重复输出：\n" + json.dumps({"title": "不应采用"}, ensure_ascii=False)
    )
    assert extract_json(response) == first


@pytest.mark.asyncio
async def test_m3_repairs_malformed_json_with_bounded_followup(monkeypatch):
    requests = []

    def handler(request):
        body = json.loads(request.content)
        requests.append(body)
        assert "response_format" not in body
        assert body["max_completion_tokens"] == 16000
        assert body["thinking"] == {"type": "disabled"}
        if len(requests) == 1:
            content = '{"title":"测试" "shots":[]}'
        else:
            assert "JSON 语法修复器" in body["messages"][0]["content"]
            content = '{"title":"测试","shots":[]}'
        return httpx.Response(200, json={
            "choices": [{"finish_reason": "stop", "message": {"content": content}}],
        })

    provider = OpenAIM3Provider()
    transport = httpx.MockTransport(handler)
    monkeypatch.setattr(settings, "m3_model", "MiniMax-M3")
    monkeypatch.setattr(settings, "m3_json_repair_attempts", 2)
    monkeypatch.setattr(
        provider,
        "_client",
        lambda: httpx.AsyncClient(base_url="https://api.minimax.io/v1/", transport=transport),
    )

    result = await provider._call([{"type": "text", "text": "返回 JSON"}])
    assert result == {"title": "测试", "shots": []}
    assert len(requests) == 2


def test_invalid_and_oversized_uploads_are_rejected(client, monkeypatch):
    invalid = client.post(
        "/api/projects",
        data={"name": "商品", "selling_points": "有效卖点"},
        files={"image": ("fake.jpg", b"not-an-image", "image/jpeg")},
    )
    assert invalid.status_code == 400

    monkeypatch.setattr(settings, "max_upload_bytes", 8)
    oversized = client.post(
        "/api/projects",
        data={"name": "商品", "selling_points": "有效卖点"},
        files={"image": ("large.png", PNG, "image/png")},
    )
    assert oversized.status_code == 413


def test_single_shot_retry_preserves_attempt_count(client, monkeypatch):
    class FailOneSubmissionH3:
        def __init__(self):
            self.submissions = {}

        async def submit(self, shot, image_path, aspect_ratio):
            count = self.submissions.get(shot["id"], 0) + 1
            self.submissions[shot["id"]] = count
            if shot["position"] == 1 and count == 1:
                raise TimeoutError("synthetic submit timeout")
            return f"task-{shot['id']}-{count}"

        async def poll(self, task_id):
            return {"status": "succeeded", "output_url": f"https://example.test/{task_id}.mp4"}

    provider = FailOneSubmissionH3()
    monkeypatch.setattr(service, "h3_provider", lambda: provider)
    project_id = create_project(client)
    assert client.post(f"/api/projects/{project_id}/confirm").status_code == 200
    failed = wait_for_status(client, project_id, "partial_failed")
    failed_shot = next(shot for shot in failed["shots"] if shot["status"] == "failed")
    assert failed_shot["attempts"] == 1

    response = client.post(f"/api/projects/{project_id}/shots/{failed_shot['id']}/retry")
    assert response.status_code == 200
    completed = wait_for_status(client, project_id, "completed")
    retried = next(shot for shot in completed["shots"] if shot["id"] == failed_shot["id"])
    assert retried["attempts"] == 2


def test_completed_shot_can_retry_with_an_updated_prompt(client, monkeypatch):
    class RecordingH3:
        def __init__(self):
            self.submissions = []

        async def submit(self, shot, image_path, aspect_ratio):
            self.submissions.append((shot["id"], shot["prompt"]))
            return f"task-{shot['id']}-{len(self.submissions)}"

        async def poll(self, task_id):
            return {"status": "succeeded", "output_url": f"https://example.invalid/{task_id}.mp4"}

    provider = RecordingH3()
    monkeypatch.setattr(service, "h3_provider", lambda: provider)
    project_id = create_project(client)
    assert client.post(f"/api/projects/{project_id}/confirm").status_code == 200
    completed = wait_for_status(client, project_id, "completed")
    shot = completed["shots"][0]
    original_prompt = shot["prompt"]
    revised_prompt = "保持商品包装完全一致，固定镜头并减少运动幅度，使用柔和自然光重新生成。"

    retried = client.post(
        f"/api/projects/{project_id}/shots/{shot['id']}/retry",
        json={"prompt": revised_prompt},
    )
    assert retried.status_code == 200, retried.text
    assert retried.json()["prompt"] == revised_prompt
    completed_again = wait_for_status(client, project_id, "completed")
    updated = next(item for item in completed_again["shots"] if item["id"] == shot["id"])
    assert updated["prompt"] == revised_prompt
    assert updated["attempts"] == 2
    assert original_prompt != revised_prompt
    assert [prompt for submitted_id, prompt in provider.submissions if submitted_id == shot["id"]][-1] == revised_prompt
    planned = next(item for item in completed_again["script"]["shots"] if item["position"] == shot["position"])
    assert planned["prompt"] == revised_prompt


def test_resume_retries_only_failed_shots(client, monkeypatch):
    class FailOneTaskH3:
        def __init__(self):
            self.submissions = 0

        async def submit(self, shot, image_path, aspect_ratio):
            self.submissions += 1
            return f"resume-task-{self.submissions}"

        async def poll(self, task_id):
            if task_id == "resume-task-1":
                return {"status": "failed", "error": "synthetic failure"}
            return {"status": "succeeded", "output_url": f"https://example.test/{task_id}.mp4"}

    provider = FailOneTaskH3()
    monkeypatch.setattr(service, "h3_provider", lambda: provider)
    project_id = create_project(client)
    assert client.post(f"/api/projects/{project_id}/confirm").status_code == 200
    failed = wait_for_status(client, project_id, "partial_failed")
    failed_id = next(shot["id"] for shot in failed["shots"] if shot["status"] == "failed")
    succeeded_ids = {shot["id"] for shot in failed["shots"] if shot["status"] == "succeeded"}

    response = client.post(f"/api/projects/{project_id}/resume")
    assert response.status_code == 200
    completed = wait_for_status(client, project_id, "completed")
    attempts = {shot["id"]: shot["attempts"] for shot in completed["shots"]}
    assert attempts[failed_id] == 2
    assert all(attempts[shot_id] == 1 for shot_id in succeeded_ids)


def test_poll_transport_error_does_not_resubmit(client, monkeypatch):
    class FlakyPollH3:
        def __init__(self):
            self.submissions = 0
            self.polled = set()

        async def submit(self, shot, image_path, aspect_ratio):
            self.submissions += 1
            return f"task-{self.submissions}"

        async def poll(self, task_id):
            if task_id not in self.polled:
                self.polled.add(task_id)
                raise TimeoutError("temporary timeout")
            return {"status": "succeeded", "output_url": f"https://example.test/{task_id}.mp4"}

    provider = FlakyPollH3()
    monkeypatch.setattr(service, "h3_provider", lambda: provider)
    project_id = create_project(client)
    shot_count = len(client.get(f"/api/projects/{project_id}").json()["shots"])
    assert client.post(f"/api/projects/{project_id}/confirm").status_code == 200
    completed = wait_for_status(client, project_id, "completed")
    assert provider.submissions == shot_count
    assert all(shot["attempts"] == 1 for shot in completed["shots"])


@pytest.mark.asyncio
async def test_minimax_h3_v2_request_and_response_contract(tmp_path, monkeypatch):
    calls = []
    visual_roles = []
    visual_role_sets = []

    def handler(request):
        calls.append(request)
        assert request.headers["authorization"] == "Bearer sk-api-test"
        if request.method == "POST":
            assert request.url.path == "/v2/video_generation"
            body = json.loads(request.content)
            assert body["model"] == "MiniMax-H3"
            assert body["resolution"] == "768P"
            assert body["ratio"] == "9:16"
            assert body["content"][0]["type"] == "text"
            assert body["content"][0]["text"].startswith(("商品旋转展示，保持包装一致", "从上一镜动作自然延续"))
            assert "不要生成任何背景音乐或旋律" in body["content"][0]["text"]
            visual_roles.append(body["content"][1]["role"])
            visual_role_sets.append([item["role"] for item in body["content"][1:]])
            assert body["content"][1]["image_url"]["url"].startswith("data:image/png;base64,")
            return httpx.Response(200, json={"task_id": "h3-task-1"})
        assert request.url.path == "/v2/query/video_generation/h3-task-1"
        return httpx.Response(200, json={"task": {"id": "h3-task-1", "status": "succeeded", "content": {"url": "https://example.test/output.mp4"}}})

    transport = httpx.MockTransport(handler)
    provider = HttpH3Provider()
    monkeypatch.setattr(settings, "h3_api_key", "sk-api-test")
    monkeypatch.setattr(settings, "h3_base_url", "https://api.minimax.io")
    monkeypatch.setattr(settings, "h3_model", "MiniMax-H3")
    monkeypatch.setattr(settings, "h3_resolution", "768P")
    monkeypatch.setattr(settings, "h3_submit_path", "/v2/video_generation")
    monkeypatch.setattr(settings, "h3_status_path", "/v2/query/video_generation/{task_id}")
    monkeypatch.setattr(provider, "_client", lambda: httpx.AsyncClient(base_url="https://api.minimax.io", transport=transport, headers={"Authorization": "Bearer sk-api-test"}))
    image = tmp_path / "product.png"
    image.write_bytes(PNG)

    task_id = await provider.submit({"prompt": "商品旋转展示，保持包装一致", "duration": 5}, str(image), "9:16")
    multi_task_id = await provider.submit(
        {"prompt": "商品旋转展示，保持包装一致", "duration": 5}, str(image), "9:16",
        reference_image_paths=[str(image), str(image)],
    )
    result = await provider.poll(task_id)
    chained_task_id = await provider.submit(
        {"prompt": "从上一镜动作自然延续", "duration": 5}, str(image), "9:16",
        first_frame_path=str(image),
    )

    assert task_id == "h3-task-1"
    assert multi_task_id == "h3-task-1"
    assert chained_task_id == "h3-task-1"
    assert result == {"status": "succeeded", "output_url": "https://example.test/output.mp4"}
    assert visual_roles == ["reference_image", "reference_image", "first_frame"]
    assert visual_role_sets == [["reference_image"], ["reference_image", "reference_image"], ["first_frame"]]
    assert len(calls) == 4
