from app.models import QAReport, Storyboard, VideoQAReport
from app.quality import deterministic_qa, merge_qa
from app.service import normalize_storyboard_continuity


def test_blocks_absolute_claim():
    board = Storyboard(title="测试", style="电商", shots=[
        {"position":1,"title":"a","duration":5,"prompt":"商品稳定地放在桌面中央，保持包装一致","voiceover":"100%治愈","overlay_text":"最好"},
        {"position":2,"title":"b","duration":5,"prompt":"商品近景展示卖点，保持包装一致","voiceover":"补水","overlay_text":"补水"},
        {"position":3,"title":"c","duration":5,"prompt":"商品结尾定格展示，保持包装一致","voiceover":"精华","overlay_text":"精华"},
    ])
    report = deterministic_qa(board, "补水精华")
    assert not report.passed
    assert any(i.code == "risky_claim" for i in report.issues)


def test_valid_board_passes():
    board = Storyboard(title="测试", style="电商", shots=[
        {"position":i,"title":str(i),"duration":5,"prompt":f"补水精华商品镜头 {i}，保持包装外观一致","voiceover":"补水精华","overlay_text":"补水精华"}
        for i in range(1,4)
    ])
    assert deterministic_qa(board, "补水精华").passed


def test_model_rejection_cannot_be_ignored():
    local = QAReport(passed=True, score=100, issues=[])
    model = QAReport(passed=False, score=20, issues=[])
    assert not merge_qa(local, model).passed


def test_score_and_hard_errors_are_the_single_qa_gate():
    local = QAReport(passed=True, score=94, issues=[])
    inconsistent_model = QAReport(
        passed=False, score=82,
        issues=[{"severity": "warning", "code": "polish", "message": "可优化布光"}],
    )
    assert merge_qa(local, inconsistent_model, pass_score=70).passed


def test_model_info_severity_is_kept_as_warning_instead_of_rejecting_report():
    report = QAReport.model_validate({
        "passed": True,
        "score": 91,
        "issues": [{"severity": "info", "code": "suggestion", "message": "可优化运镜"}],
    })
    video_report = VideoQAReport.model_validate({
        "passed": True,
        "score": 90,
        "issues": [{"severity": "info", "code": "minor", "message": "轻微建议"}],
        "recommendation": "可以采用",
    })
    assert report.issues[0].severity == "warning"
    assert video_report.issues[0].severity == "warning"


def test_grounding_requires_a_phrase_not_one_character():
    board = Storyboard(title="测试", style="电商", shots=[
        {"position":i,"title":str(i),"duration":5,"prompt":f"商品在水边展示镜头 {i}，保持包装外观一致","voiceover":"清爽体验","overlay_text":"清爽"}
        for i in range(1,4)
    ])
    report = deterministic_qa(board, "补水精华")
    missing = [issue for issue in report.issues if issue.code == "missing_selling_point"]
    assert len(missing) == 1
    assert missing[0].shot_position is None


def test_verbose_model_style_is_safely_bounded():
    verbose_style = "Clean botanical skincare commercial with contemplative pacing. " * 20
    board = Storyboard(title="测试", style=verbose_style, shots=[
        {"position": i, "title": str(i), "duration": 5,
         "prompt": f"补水精华商品镜头 {i}，保持包装外观一致"}
        for i in range(1, 4)
    ])
    assert len(board.style) == 500
    assert board.style == verbose_style.strip()[:500]


def test_continuity_fields_are_not_duplicated_into_prompt():
    english = Storyboard(title="Test", style="Clean commercial", shots=[{
        "position": 1, "title": "Opening", "duration": 5,
        "prompt": "The product is introduced in a clean studio composition.",
    }, {
        "position": 2, "title": "Hero", "duration": 5,
        "prompt": "Full product bottle centered on a warm cream seamless background with a slow camera push-in.",
        "entry_action": "硬切回到白色纯净背景的瓶身正面",
        "exit_action": "镜头缓慢推近并稳定停留",
    }])
    normalized_english = normalize_storyboard_continuity(english)
    assert "Shot continuity:" not in normalized_english.shots[1].prompt
    assert "硬切" not in normalized_english.shots[1].prompt
    assert "白色纯净背景" not in normalized_english.shots[1].prompt

    chinese = Storyboard(title="测试", style="干净广告", shots=[{
        "position": 1, "title": "开场", "duration": 5,
        "prompt": "商品在干净的摄影棚构图中出现，保持包装外观一致。",
    }, {
        "position": 2, "title": "主视觉", "duration": 5,
        "prompt": "完整商品瓶身位于暖奶油色背景中央，镜头缓慢推近并保持包装一致。",
        "entry_action": "Open on a pure white background",
        "exit_action": "Hold on a stable brand frame",
    }])
    normalized_chinese = normalize_storyboard_continuity(chinese)
    assert "镜头衔接：" not in normalized_chinese.shots[1].prompt
    assert "pure white" not in normalized_chinese.shots[1].prompt
