# 商品图文生成短视频流水线

一个可直接运行的 FastAPI MVP：用 M3 把项目级 Reference Library 与卖点转换为结构化分镜，先做规则 + M3 双重质量检查，用户确认后逐镜调用 H3。项目、参考素材、镜头和质检结果写入 SQLite，生成视频缓存到本地，因此可从最近项目重新打开、继续编辑、分镜级重试、失败续跑和服务重启恢复。

## 流程

`建立 Reference Library → M3 生成分镜 → 自动质检并定向修订（最多 3 版，保留最高分）→ 人工修改/确认 → H3 多参考图或尾帧延续生成 → 本地缓存 + M3 成片质检 → FFmpeg 转场合成预览 → 人工采用或单镜重试`

关键约束：

- 未确认的脚本绝不会调用 H3。
- 新项目可上传 1–9 张参考图，并为每张图设置主体类型、用途标签、优先级、自由描述和必须保持的特征。
- Reference Library 支持旧项目自动迁移以及创建后的新增、查看、编辑和删除；生成中的项目会锁定素材修改。
- M3 为每镜推荐参考素材，用户可逐镜覆盖；独立镜头向 H3 提交所选多张 `reference_image`，尾帧镜头则只提交 `first_frame`。
- 每个镜头有独立状态、尝试次数、远端任务 ID、错误信息和输出 URL。
- 启动时自动恢复 `queued/submitted/running` 镜头。
- 确认前可连续修改多个分镜；保存不会清除旧质检，用户点击“重新评估分镜”后才统一执行规则检查与 M3 质检。
- 确认前可让 M3 只重写指定镜头的文案；调用时以数据库中的整套最新 Storyboard 为上下文，并补充前后镜头、Reference、卖点和现有质检问题，其他镜头及转场/尾帧/参考图配置保持不变。
- `retry` 可由用户重试失败镜头或对质检结果不满意的已完成镜头，不会清零累计尝试次数。
- `resume` 仅继续未完成镜头。
- M3 检查分为确定性规则检查和可选的模型复核；严重问题阻止确认。
- 新脚本未达到 70 分或包含硬错误时，M3 会针对质检问题自动修订，最多评估 3 个候选版本；达标立即停止，仍未达标则展示最高分版本供人工修改或采纳，绝不会自动调用 H3。
- 项目名称只作为内部工作名，包装品牌与文字以 Reference Library 图片为准；包装原文和结构枚举不再被误判为语言混用。
- 质检未通过时可由用户填写原因并人工采纳；采纳只解除门禁，仍需再次确认才会调用 H3。
- H3 成片完成后会尝试缓存到 `data/videos/{project_id}/`，并由 M3 给出分数、问题和重试建议。
- 成片质检不会触发自动重生成；即使评分较低，是否重试也始终由用户决定。
- M3 会为镜头边界选择直接切、动作匹配、短叠化、淡出换场或尾帧延续；换人物/换场景不会强行使用尾帧。
- 每个非首镜都提供统一的“插入上一镜尾帧”开关：初始值来自 M3，人工可在确认前或成片后覆盖；设置会持久化，并在下一次生成该镜头时生效。
- `carry_last_frame` 镜头会等待上一镜成功，从本地视频提取干净尾帧并作为 H3 下一镜的首帧；其他镜头仍可并行生成。
- 全部镜头完成后，FFmpeg 会统一转场并对音频做交叉淡化，生成 `data/previews/{project_id}.mp4`。合成失败时 UI 回退到逐镜串播。
- 重试已完成镜头前可编辑当前 H3 Prompt；保存后新 Prompt 会同步写回分镜脚本并用于本次生成。
- 成片后仍可编辑单镜的标题、时长、Prompt、旁白、屏显、转场和开场/收尾动作；修改后的镜头会进入“待重生成”，旧版整片导出立即失效，所有修改镜头重新生成完成后自动重建预览。
- 左侧项目栏始终列出最近 50 个项目；刷新后会恢复上次打开的项目，也可以从项目栏永久删除项目及其本地素材。

## 快速启动（Mock 模式）

需要 Python 3.9 或更高版本。
整体预览合成与尾帧提取还需要本机安装 `ffmpeg` 和 `ffprobe`。

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env
uvicorn app.main:app --reload --port 8000
```

打开 <http://127.0.0.1:8000>。Mock 模式不需要密钥，适合先验收完整流程。

## 接入 MiniMax M3

M3 适配器使用 OpenAI-compatible `chat/completions` 多模态格式：

```env
M3_MODE=openai
M3_BASE_URL=https://api.minimax.io/v1
M3_API_KEY=sk-api-your-key
M3_MODEL=MiniMax-M3
M3_TIMEOUT_SECONDS=300
M3_JSON_REPAIR_ATTEMPTS=2
M3_ENABLE_REVIEW=true
M3_ENABLE_VIDEO_REVIEW=true
M3_STORYBOARD_MAX_ATTEMPTS=3
M3_STORYBOARD_PASS_SCORE=70
```

上面是国际站地址。中国大陆开放平台签发的 Key 使用 `M3_BASE_URL=https://api.minimaxi.com/v1`。

模型必须返回 JSON。程序会从 Markdown code fence 中自动提取 JSON，并再次做本地校验。

## 接入 MiniMax H3

H3 适配器使用 MiniMax 官方 Video Generation V2 异步协议。普通 `sk-api` Key 可同时用于 M3 与 H3，也可以只填写 `MINIMAX_API_KEY` 作为共享 Key：

```env
H3_MODE=http
H3_BASE_URL=https://api.minimax.io
H3_API_KEY=sk-api-your-key
H3_MODEL=MiniMax-H3
H3_RESOLUTION=768P
H3_SUBMIT_PATH=/v2/video_generation
H3_STATUS_PATH=/v2/query/video_generation/{task_id}
H3_NATIVE_VOICEOVER=true
H3_DISABLE_BACKGROUND_MUSIC=true
```

上面是国际站地址。中国大陆开放平台的 H3 使用 `H3_BASE_URL=https://api.minimax.cn`；M3 与 H3 的国内域名不同。

提交请求：

```json
{
  "model": "MiniMax-H3",
  "content": [
    {"type": "text", "text": "..."},
    {"type": "image_url", "image_url": {"url": "data:image/jpeg;base64,..."}, "role": "reference_image"}
  ],
  "resolution": "768P",
  "duration": 5,
  "ratio": "9:16"
}
```

提交成功后保存 `task_id`，随后查询 `/v2/query/video_generation/{task_id}`；成功视频地址读取自 `task.content.url`。H3 按量付费权限和余额需要在 MiniMax Open Platform 中开通。

默认使用 H3 原生音频生成每镜画外音，并明确禁止各镜头独立生成背景音乐，避免合成后出现配乐跳变。该模式不调用独立 TTS；H3 原生旁白的音色与逐字准确度仍由模型决定。可通过 `H3_NATIVE_VOICEOVER=false` 关闭旁白，或通过 `H3_DISABLE_BACKGROUND_MUSIC=false` 允许 H3 自行生成单镜音乐。

## API

| 方法 | 路径 | 作用 |
|---|---|---|
| POST | `/api/projects` | 上传 1–9 张 Reference 图片、素材标记与卖点并创建项目 |
| GET | `/api/projects` | 查看最近项目列表 |
| GET | `/api/projects/{id}` | 查看脚本、检查结果和镜头状态 |
| DELETE | `/api/projects/{id}` | 永久删除项目、镜头、上传图片及本地视频 |
| GET | `/api/projects/{id}/references/{asset_id}/image` | 读取 Reference 图片 |
| POST | `/api/projects/{id}/references` | 向 Reference Library 添加图片 |
| PATCH | `/api/projects/{id}/references/{asset_id}` | 修改素材分类、描述、约束或设为主参考 |
| DELETE | `/api/projects/{id}/references/{asset_id}` | 删除素材；最后一张不可删除 |
| PATCH | `/api/projects/{id}/shots/{shot_id}` | 编辑草稿或成片镜头的完整结构；成片修改后标记为待重生成 |
| POST | `/api/projects/{id}/shots/{shot_id}/rewrite-script` | 让 M3 基于整套最新上下文只重写一个未确认镜头的文案 |
| POST | `/api/projects/{id}/recheck-script` | 手动对当前全部分镜执行一次 M3 重新评估 |
| PATCH | `/api/projects/{id}/shots/{shot_id}/continuity` | 开启/关闭下一次生成时的上一镜尾帧衔接 |
| PATCH | `/api/projects/{id}/shots/{shot_id}/references` | 人工设置该镜头下一次生成使用的参考图 |
| GET | `/api/projects/{id}/shots/{shot_id}/video` | 播放本地缓存视频 |
| GET | `/api/projects/{id}/preview` | 播放带转场和音频衔接的合成预览 |
| GET | `/api/projects/{id}/export` | 下载已合成的完整 MP4 成片 |
| POST | `/api/projects/{id}/regenerate-script` | 重做分镜与质检 |
| POST | `/api/projects/{id}/override-qa` | 人工采纳未通过质检的当前脚本 |
| POST | `/api/projects/{id}/confirm` | 用户确认并开始生成 |
| POST | `/api/projects/{id}/resume` | 继续所有未完成镜头 |
| POST | `/api/projects/{id}/shots/{shot_id}/retry` | 使用可选的新 Prompt 重试指定失败或已完成镜头 |

## 测试

```bash
pytest -q
```

## 生产化建议

MVP 的后台 worker 在 API 进程内运行。多实例生产部署时建议将 worker 换成 Celery/RQ/Temporal，并把 SQLite 换成 PostgreSQL；镜头状态机和 Provider 接口可原样保留。
