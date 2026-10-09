import base64
import json
import re
import uuid
from pathlib import Path
from typing import Optional

import httpx

from .config import settings
from .models import QAReport, ShotRewriteResult, Storyboard, VideoQAReport


def minimax_key(preferred: str) -> str:
    """Allow one shared MiniMax key while retaining per-provider overrides."""
    return preferred or settings.minimax_api_key or settings.m3_api_key or settings.h3_api_key


def data_url(path: str) -> str:
    p = Path(path)
    mime = {
        ".png": "image/png",
        ".webp": "image/webp",
        ".jpg": "image/jpeg",
        ".jpeg": "image/jpeg",
    }.get(p.suffix.lower())
    if not mime:
        raise ValueError(f"不支持的图片格式：{p.suffix}")
    return f"data:{mime};base64,{base64.b64encode(p.read_bytes()).decode()}"


def extract_json(text: str) -> dict:
    text = re.sub(r"^```(?:json)?\s*|\s*```$", "", text.strip(), flags=re.I)
    start = text.find("{")
    if start < 0:
        raise ValueError("模型未返回 JSON 对象")
    # raw_decode stops at the end of the first complete object. M3 can
    # occasionally append an explanation or a duplicate JSON block even when
    # JSON output was requested; slicing through the last `}` turns that valid
    # first object into an `Extra data` error.
    value, _ = json.JSONDecoder().raw_decode(text, start)
    if not isinstance(value, dict):
        raise ValueError("模型返回的 JSON 顶层必须是对象")
    return value


def h3_prompt_with_audio_direction(shot: dict) -> str:
    """Add an explicit native-audio policy to every independently generated clip."""
    prompt = str(shot.get("prompt") or "").strip()
    voiceover = str(shot.get("voiceover") or "").strip()
    chinese = bool(re.search(r"[\u3400-\u9fff]", prompt + voiceover))
    directions = []
    if settings.h3_native_voiceover and voiceover:
        if chinese:
            directions.append(
                f"生成画外音，由同一位音色中性偏温暖、自然克制的普通话商业广告旁白逐字朗读：‘{voiceover}’。"
                "旁白清晰稳定，不要求画面人物对口型，不得改写、增删或重复文案。"
            )
        else:
            directions.append(
                f'Generate off-screen narration in the same warm, neutral, restrained commercial voice, reading exactly: "{voiceover}". '
                "Keep the narration clear and stable; do not lip-sync an on-screen person and do not rewrite, add, omit, or repeat words."
            )
    elif settings.h3_native_voiceover:
        directions.append("不要生成旁白。" if chinese else "Do not generate narration.")
    if settings.h3_disable_background_music:
        directions.append(
            "不要生成任何背景音乐或旋律，只保留轻微、真实且与画面匹配的环境声和必要音效。"
            if chinese else
            "Do not generate background music or melody; retain only subtle, realistic ambience and necessary sound effects that match the scene."
        )
    if not directions:
        return prompt
    heading = "音频要求：" if chinese else "Audio requirements: "
    return f"{prompt}\n{heading}{''.join(directions)}"


class MockM3Provider:
    async def storyboard(self, name: str, points: str, assets: list[dict]) -> Storyboard:
        points_list = [p.strip() for p in re.split(r"[，,；;\n]", points) if p.strip()] or ["突出产品价值"]
        reference_ids = [asset["id"] for asset in assets if asset.get("priority") == "core"][:4] or [asset["id"] for asset in assets[:4]]
        shot_count = min(5, max(3, len(points_list) + 1))
        shots = []
        for i in range(shot_count):
            point = points_list[min(i, len(points_list) - 1)]
            shots.append({"position": i + 1, "title": ["视觉钩子", "质感特写", "核心卖点", "使用场景", "品牌收束"][i],
              "duration": 5, "prompt": f"竖屏商业短片，保持参考商品外观、包装文字与品牌标识一致。{name}，{point}。镜头平稳，真实光影，禁止改变商品结构。",
              "voiceover": f"{name}，{point}", "overlay_text": point[:16],
              "continuity_mode": "independent", "transition_type": "cut" if i == 0 else "dissolve",
              "transition_duration_ms": 0 if i == 0 else 250,
              "entry_action": "画面自然建立", "exit_action": "动作自然收束", "continuity_group": None,
              "reference_asset_ids": reference_ids})
        return Storyboard(title=f"{name} 商品短片", aspect_ratio="9:16", style="干净、高级、真实电商广告", shots=shots)

    async def review(self, board: Storyboard, points: str, assets: list[dict]) -> QAReport:
        return QAReport(passed=True, score=92, issues=[])

    async def revise_storyboard(self, board: Storyboard, name: str, points: str,
                                assets: list[dict], qa: QAReport) -> Storyboard:
        return board.model_copy(deep=True)

    async def rewrite_shot(self, board: Storyboard, position: int, name: str,
                           points: str, assets: list[dict], qa: Optional[QAReport],
                           instruction: str) -> ShotRewriteResult:
        shot = next(item for item in board.shots if item.position == position)
        direction = instruction.strip() or "优化画面表达并保持卖点清晰"
        return ShotRewriteResult(
            title=shot.title,
            prompt=f"{shot.prompt.rstrip()} 调整要求：{direction}。"[:1500],
            voiceover=shot.voiceover,
            overlay_text=shot.overlay_text,
            entry_action=shot.entry_action,
            exit_action=shot.exit_action,
        )

    async def review_video(self, shot: dict, video_url: str, image_path: str) -> VideoQAReport:
        return VideoQAReport(passed=True, score=91, issues=[], recommendation="成片与分镜匹配，可以采用。")


class OpenAIM3Provider:
    def _client(self) -> httpx.AsyncClient:
        api_key = minimax_key(settings.m3_api_key)
        headers = {"Authorization": f"Bearer {api_key}"} if api_key else {}
        return httpx.AsyncClient(
            base_url=f"{settings.m3_base_url.rstrip('/')}/",
            timeout=settings.m3_timeout_seconds,
            headers=headers,
        )

    def _request_body(self, messages, temperature: float) -> dict:
        body = {
            "model": settings.m3_model,
            "messages": messages,
            "temperature": temperature,
            "max_completion_tokens": 16000,
        }
        # MiniMax documents `thinking.disabled` for M3. Other model families
        # may ignore it, while M3.1 Preview rejects it.
        if settings.m3_model == "MiniMax-M3":
            body["thinking"] = {"type": "disabled"}
        return body

    async def _response_text(self, client: httpx.AsyncClient, messages, temperature: float) -> str:
        r = await client.post(
            "chat/completions",
            json=self._request_body(messages, temperature),
        )
        r.raise_for_status()
        obj = r.json()
        choice = obj["choices"][0]
        if choice.get("finish_reason") == "length":
            raise ValueError("M3 输出达到长度上限，JSON 可能不完整")
        return choice["message"]["content"]

    async def _call(self, content, temperature=0.2) -> dict:
        async with self._client() as client:
            raw = await self._response_text(
                client, [{"role": "user", "content": content}], temperature,
            )
            try:
                return extract_json(raw)
            except json.JSONDecodeError as first_error:
                error = first_error
                for attempt in range(max(0, settings.m3_json_repair_attempts)):
                    repair_prompt = (
                        "你是严格的 JSON 语法修复器。下面的文本本应是一个 JSON 对象，但存在语法错误。"
                        "只修复引号、逗号、括号、转义或截断等 JSON 语法，不要改写字段含义，不要增加解释，"
                        "只返回一个完整 JSON 对象。\n"
                        f"解析错误：{error}\n待修复内容：\n{raw}"
                    )
                    repaired = await self._response_text(
                        client,
                        [{"role": "user", "content": repair_prompt}],
                        0,
                    )
                    try:
                        return extract_json(repaired)
                    except json.JSONDecodeError as repair_error:
                        error = repair_error
                        raw = repaired
                raise error

    async def storyboard(self, name: str, points: str, assets: list[dict]) -> Storyboard:
        manifest = [{key: asset[key] for key in ("id", "asset_type", "subject_name", "view_tags", "priority", "description", "constraints")} for asset in assets]
        prompt = f"""你是电商短视频导演。依据商品图和卖点生成 3-6 个镜头的分镜 JSON。
项目工作名：{name}\n卖点：{points}
参考素材清单：{json.dumps(manifest, ensure_ascii=False)}
要求：9:16；每镜 4-15 秒；prompt 必须明确商品外观一致性、动作、运镜、光线；不得捏造图中不可证实的信息或绝对化功效。
真实性要求：项目工作名只是内部主题，不等于包装品牌或产品全名，不得把它印在瓶身、当作 Logo 或冒充参考图中的包装文字。品牌、Logo、容量及包装文字只能逐字使用参考图中清晰可见的原文；无法辨认时不要猜测或补写。卖点应尽量用质地、延展、使用动作或适用场景进行视觉佐证；功效文案使用克制的体验表达。
语言要求：title、style、prompt、voiceover、overlay_text、entry_action、exit_action 的自然语言必须统一使用简体中文；品牌名、Logo、包装原文必须保留原语言并用引号标出。cut、match_cut、dissolve、dip_to_black、independent、carry_last_frame 是 JSON 枚举，不属于自然语言混用。prompt 不得复述“开场动作/收尾动作”模板，也不得复制 entry_action/exit_action；衔接动作只放在对应结构字段中。prompt 与 entry_action/exit_action 对背景颜色、场景、主体状态、光线和运动方向的描述必须一致，不能互相冲突。
同时按专业广告剪辑规划镜头边界：只有同场景、同主体且动作真正连续时 continuity_mode 才用 carry_last_frame；换人物、换场景或品牌定帧必须用 independent。transition_type 从 cut、match_cut、dissolve、dip_to_black 选择。第一镜必须 independent + cut + 0ms；其余转场通常 40-500ms。entry_action/exit_action 描述衔接动作，continuity_group 标记同一连续场景，否则为 null。
每个 independent 镜头从清单中选择 1-4 个最相关 reference_asset_ids；carry_last_frame 镜头可为空。只可使用清单中真实存在的 id。
仅返回：{{\"title\":str,\"aspect_ratio\":\"9:16\",\"style\":str,\"shots\":[{{\"position\":1,\"title\":str,\"duration\":5,\"prompt\":str,\"voiceover\":str,\"overlay_text\":str,\"continuity_mode\":\"independent或carry_last_frame\",\"transition_type\":\"cut或match_cut或dissolve或dip_to_black\",\"transition_duration_ms\":0,\"entry_action\":str,\"exit_action\":str,\"continuity_group\":str或null,\"reference_asset_ids\":[str]}}]}}"""
        content = [{"type": "text", "text": prompt}]
        content.extend({"type": "image_url", "image_url": {"url": data_url(asset["file_path"])}} for asset in assets[:9])
        obj = await self._call(content)
        return Storyboard.model_validate(obj)

    async def revise_storyboard(self, board: Storyboard, name: str, points: str,
                                assets: list[dict], qa: QAReport) -> Storyboard:
        manifest = [{key: asset[key] for key in ("id", "asset_type", "subject_name", "view_tags", "priority", "description", "constraints")} for asset in assets]
        prompt = f"""你是电商短视频分镜修订导演。根据质检报告定向修订当前分镜，并返回一份完整 Storyboard JSON。
项目工作名：{name}
卖点：{points}
参考素材清单：{json.dumps(manifest, ensure_ascii=False)}
当前分镜：{board.model_dump_json()}
质检报告：{qa.model_dump_json()}

修订规则：
1. 优先修复全部 error，再修复会影响生成稳定性、真实性、连续性和广告合规的 warning；保留没有问题的镜头与叙事结构。
2. 项目工作名不是包装品牌。包装、Logo、容量只能使用参考图中清晰可见的原文，不得翻译、改写或把工作名写上包装。
3. 用可见的质地、使用动作或场景佐证主要卖点；不要加入参考图无法支持的结构、成分外观或医疗效果。
4. 全部自然语言统一使用简体中文；包装原文可以保留原语言。JSON 枚举词不算语言混用。
5. prompt 不得复制 entry_action/exit_action，也不得含“镜头衔接：”“开场动作：”“收尾动作：”模板。三者的场景、光线、主体和运动必须一致。
6. 只可使用素材清单中真实存在的 reference_asset_ids。每镜 4-15 秒，总时长不超过 60 秒，屏显尽量不超过 18 个字符。
只返回与首次生成完全相同结构的 JSON 对象，不要解释。"""
        content = [{"type": "text", "text": prompt}]
        content.extend({"type": "image_url", "image_url": {"url": data_url(asset["file_path"])}} for asset in assets[:9])
        return Storyboard.model_validate(await self._call(content, 0.1))

    async def rewrite_shot(self, board: Storyboard, position: int, name: str,
                           points: str, assets: list[dict], qa: Optional[QAReport],
                           instruction: str) -> ShotRewriteResult:
        manifest = [{key: asset[key] for key in ("id", "asset_type", "subject_name", "view_tags", "priority", "description", "constraints")} for asset in assets]
        target = next(shot for shot in board.shots if shot.position == position)
        neighbors = [
            shot.model_dump() for shot in board.shots
            if abs(shot.position - position) <= 1
        ]
        target_issues = [
            issue.model_dump() for issue in (qa.issues if qa else [])
            if issue.shot_position in {None, position}
        ]
        prompt = f"""你是电商短视频分镜编剧。只重写指定镜头的文案，不得改动其他镜头，也不得重新规划整套结构。
项目工作名：{name}
商品卖点：{points}
用户本次要求：{instruction.strip() or '在保持原始创意方向的前提下，提高画面可生成性、卖点表达和前后衔接。'}
目标镜头位置：{position}
目标镜头当前内容：{target.model_dump_json()}
前后相邻镜头：{json.dumps(neighbors, ensure_ascii=False)}
当前完整分镜（这是唯一可信的最新上下文）：{board.model_dump_json()}
与目标镜头相关的现有质检问题：{json.dumps(target_issues, ensure_ascii=False)}
参考素材清单：{json.dumps(manifest, ensure_ascii=False)}

要求：
1. 只改写 title、prompt、voiceover、overlay_text、entry_action、exit_action；镜头序号、时长、转场类型、尾帧衔接开关、连续性分组和 reference_asset_ids 均由系统保留。
2. 必须结合前一镜和后一镜，使目标镜头的入场、收尾、主体、场景与运动方向能够自然剪辑，但不要复述或改写相邻镜头。
3. 项目工作名不是包装品牌；品牌、Logo、容量和包装文字只能使用参考图中清晰可见的原文，无法辨认时不要猜测。
4. 自然语言统一使用简体中文，包装原文可保留原语言。prompt 不得复制 entry_action/exit_action，也不得包含“镜头衔接：”“开场动作：”“收尾动作：”模板。
5. 屏显尽量不超过 18 个汉字；旁白使用克制、可验证的体验表达，不得加入医疗化、绝对化或参考素材无法支持的功效。
仅返回 JSON：{{"title":str,"prompt":str,"voiceover":str,"overlay_text":str,"entry_action":str,"exit_action":str}}"""
        content = [{"type": "text", "text": prompt}]
        content.extend({"type": "image_url", "image_url": {"url": data_url(asset["file_path"])}} for asset in assets[:9])
        return ShotRewriteResult.model_validate(await self._call(content, 0.2))

    async def review(self, board: Storyboard, points: str, assets: list[dict]) -> QAReport:
        prompt = f"""审查以下商品视频分镜。检查：是否忠于卖点、是否可能改变包装/商标、镜头是否可生成、前后连续、文案是否违规或夸大；并检查每镜 prompt、entry_action、exit_action 的自然语言是否统一，以及背景颜色、场景、主体状态、光线和运动方向是否真正矛盾。

统一判分规则：70 分及以上且没有 error 即为可用（passed=true）；优化建议可以记 warning，但不能因此判失败。error 仅用于会阻止安全生成的客观问题，例如明确改写参考包装/商标、明显违法或医疗化/绝对化宣称、结构数据无效、同一镜头存在无法同时执行的核心视觉冲突。审美偏好、可进一步优化、轻微冗余或证据不足的合规提醒只能是 warning。最多返回 8 条不重复、可执行的问题。
避免误判：项目标题可能只是内部工作名，不是包装品牌；只根据随附参考图判断包装真实性，不做外部品牌或商标推断。品牌名、Logo、容量和包装原文保留外语不算语言混用。cut、match_cut、dissolve、dip_to_black、independent、carry_last_frame 等 JSON 字段值，以及 hero、focus 等常用影视术语，不算主语言混用。不同镜头采用不同但合理的布光、背景或构图不自动构成连续性错误；只有声明为同一连续动作却发生无解释突变时才指出。
卖点：{points}\n分镜：{board.model_dump_json()}
仅返回 JSON：{{\"passed\":bool,\"score\":0到100,\"issues\":[{{\"severity\":\"error或warning\",\"code\":str,\"message\":str,\"shot_position\":整数或null}}]}}"""
        content = [{"type": "text", "text": prompt}]
        content.extend({"type": "image_url", "image_url": {"url": data_url(asset["file_path"])}} for asset in assets[:9])
        return QAReport.model_validate(await self._call(content, 0))

    async def review_video(self, shot: dict, video_url: str, image_path: str) -> VideoQAReport:
        prompt = f"""你是电商 AI 视频质检员。对照商品原图、分镜要求与生成视频进行审查。
分镜标题：{shot['title']}
分镜提示词：{shot['prompt']}
旁白：{shot['voiceover']}
屏显文案：{shot['overlay_text']}
检查商品外观/包装/颜色一致性、文字与 Logo 稳定性、形变闪烁穿模、动作运镜、画面质量及是否符合分镜。
仅返回 JSON：{{"passed":bool,"score":0到100,"issues":[{{"severity":"error或warning","code":str,"message":str}}],"recommendation":str,"retry_prompt":str}}。
retry_prompt 应是可直接用于重新生成该镜头的改进提示词；无需重试时返回空字符串。"""
        content = [
            {"type": "text", "text": prompt},
            {"type": "image_url", "image_url": {"url": data_url(image_path), "detail": "default"}},
            {"type": "video_url", "video_url": {"url": video_url, "detail": "default"}},
        ]
        return VideoQAReport.model_validate(await self._call(content, 0))


class MockH3Provider:
    async def submit(self, shot: dict, image_path: str, aspect_ratio: str, first_frame_path: str = None, reference_image_paths: list[str] = None) -> str:
        return f"mock-{uuid.uuid4().hex}"

    async def poll(self, task_id: str) -> dict:
        return {"status": "succeeded", "output_url": f"https://example.invalid/mock/{task_id}.mp4"}


class HttpH3Provider:
    def _client(self) -> httpx.AsyncClient:
        api_key = minimax_key(settings.h3_api_key)
        headers = {"Authorization": f"Bearer {api_key}"} if api_key else {}
        return httpx.AsyncClient(base_url=f"{settings.h3_base_url.rstrip('/')}/", timeout=120, headers=headers)

    async def submit(self, shot: dict, image_path: str, aspect_ratio: str, first_frame_path: str = None, reference_image_paths: list[str] = None) -> str:
        visuals = ([{"type": "image_url", "image_url": {"url": data_url(first_frame_path)}, "role": "first_frame"}]
                   if first_frame_path else
                   [{"type": "image_url", "image_url": {"url": data_url(path)}, "role": "reference_image"}
                    for path in (reference_image_paths or [image_path])[:9]])
        body = {
            "model": settings.h3_model,
            "content": [
                {"type": "text", "text": h3_prompt_with_audio_direction(shot)},
                *visuals,
            ],
            "resolution": settings.h3_resolution,
            "duration": shot["duration"],
            "ratio": aspect_ratio,
        }
        async with self._client() as client:
            r = await client.post(settings.h3_submit_path.lstrip("/"), json=body)
        r.raise_for_status()
        obj = r.json()
        task_id = obj.get("task_id") or obj.get("id") or obj.get("data", {}).get("task_id")
        if not task_id:
            raise ValueError("H3 提交响应缺少 task_id/id")
        return str(task_id)

    async def poll(self, task_id: str) -> dict:
        path = settings.h3_status_path.format(task_id=task_id)
        async with self._client() as client:
            r = await client.get(path.lstrip("/"))
        r.raise_for_status()
        obj = r.json()
        data = obj.get("task") or obj.get("data") or obj
        state = str(data.get("status") or data.get("state") or "running").lower()
        if state in {"success", "succeeded", "completed", "done"}:
            content = data.get("content") or {}
            return {"status": "succeeded", "output_url": content.get("url") or data.get("output_url") or data.get("video_url") or data.get("url")}
        if state in {"failed", "error", "cancelled"}:
            error = data.get("error") or data.get("message") or obj.get("message") or state
            return {"status": "failed", "error": error if isinstance(error, str) else json.dumps(error, ensure_ascii=False)}
        return {"status": "running"}


def m3_provider():
    return OpenAIM3Provider() if settings.m3_mode == "openai" else MockM3Provider()


def h3_provider():
    return HttpH3Provider() if settings.h3_mode == "http" else MockH3Provider()
