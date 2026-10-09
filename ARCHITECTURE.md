# 商品短视频流水线架构

导出的图片版本：[技术架构 PNG](./technical-architecture.png) · [业务架构 PNG](./business-architecture.png)

## 1. 技术架构

```mermaid
flowchart LR
    U[浏览器用户] -->|HTTP / FormData| UI[单页 Web UI<br/>HTML + CSS + JavaScript]
    UI -->|REST API| API[FastAPI 应用]
    subgraph APP[应用进程]
      API --> SVC[业务服务层<br/>脚本生成 / 状态转换]
      API --> DB[(SQLite<br/>projects + reference_assets + shots)]
      SVC --> DB
      W[异步 Worker<br/>依赖调度 / 轮询 / 缓存 / 成片质检] --> DB
      W --> H3A[H3 Provider Adapter]
      W --> M3A
      SVC --> M3A[M3 Provider Adapter]
    end
    M3A -->|OpenAI-compatible<br/>多模态请求| M3[M3 模型服务]
    H3A -->|提交任务 / 查询状态| H3[H3 视频服务]
    API --> FS[(本地文件系统<br/>Reference 图片 + 视频缓存)]
    M3A --> FS
    H3A --> FS
    H3 -->|视频 URL| H3A
    H3A -->|下载成片| FS
    W --> FF[FFmpeg<br/>尾帧提取 / 转场与音频合成]
    FF --> FS
```

### 技术分层

| 层级 | 当前实现 | 职责 |
|---|---|---|
| 展示层 | `app/static/index.html` | Reference Library CRUD、逐镜素材选择、项目导航、整体预览、Prompt 编辑与人工重试 |
| API 层 | `app/main.py` | 素材与项目 CRUD、参数校验、本地媒体访问、状态转换入口 |
| 业务层 | `app/service.py` | 参考素材编排、连续性计划校验、依赖调度、尾帧提取、缓存、质检与合成 |
| 适配层 | `app/providers.py` | 隔离 M3/H3 的具体 HTTP 协议 |
| 质量层 | `app/quality.py` | 确定性规则检查与模型报告合并 |
| 持久层 | `app/db.py` + SQLite | 项目、Reference 资产、逐镜选择、分镜状态与远端任务 ID |
| 后台执行 | 进程内 asyncio worker | 提交 H3、轮询状态、失败恢复 |

## 2. 业务架构

```mermaid
flowchart TD
    A[上传多张图片并建立 Reference Library] --> A2[固定分类 + 自由描述 + 保持约束]
    A2 --> B[M3 选择逐镜素材并生成分镜与转场计划]
    B --> C[本地确定性规则检查]
    B --> D[M3 多模态复核]
    C --> E[合并质量报告]
    D --> E
    E -->|存在严重问题| F[质检失败]
    F --> B
    F -->|人工审阅并采纳| G
    E -->|通过| G[人工审阅分镜]
    G -->|编辑并重新质检| C
    G -->|重做| B
    G -->|确认| H[按连续性组加入 H3 队列]
    H --> P{M3 建议或人工设置<br/>需要尾帧延续?}
    P -->|否| I[使用逐镜选择的多张 Reference 图片提交 H3]
    P -->|是| R[等待上一镜并提取真实尾帧]
    R --> I2[以尾帧作为下一镜首帧提交 H3]
    I2 --> J
    I --> J[保存远端 task ID]
    J --> K[轮询 H3 状态]
    K -->|仍在运行| K
    K -->|成功| L[保存视频 URL 并缓存成片]
    K -->|失败| M[标记单镜失败]
    M -->|单镜重试| I
    M -->|批量续跑| H
    L --> Q[M3 成片评分与建议]
    Q -->|修改 Prompt 并人工重试| I
    Q -->|人工采用| N{全部镜头成功?}
    N -->|否| K
    N -->|是| X[FFmpeg 按计划合成转场与音频]
    X --> O[项目完成 / 整体预览]
```

## 3. 项目与分镜状态机

```mermaid
stateDiagram-v2
    state "项目" as Project {
      [*] --> scripting
      scripting --> awaiting_confirmation: M3 + QA 通过
      scripting --> qa_failed: QA 未通过
      scripting --> script_failed: M3/解析失败
      qa_failed --> scripting: 重做分镜
      qa_failed --> awaiting_confirmation: 人工采纳并记录原因
      script_failed --> scripting: 重做分镜
      awaiting_confirmation --> scripting: 重做分镜
      awaiting_confirmation --> generating: 用户确认
      generating --> completed: 全部分镜成功
      generating --> partial_failed: 存在失败且无运行任务
      partial_failed --> generating: 重试 / 续跑
    }
    state "分镜" as Shot {
      [*] --> draft
      draft --> queued: 用户确认
      queued --> submitting: 原子领取任务
      submitting --> submitted: 获得 H3 task ID
      submitting --> failed: 提交失败
      submitted --> running: H3 处理中
      running --> running: 继续轮询
      submitted --> succeeded: H3 完成
      running --> succeeded: H3 完成
      submitted --> failed: H3 返回失败
      running --> failed: H3 返回失败
      failed --> queued: 未超重试上限
      succeeded --> queued: 用户根据成片质检决定重试
    }
```

## 4. 当前部署边界

当前是单机 MVP：API、worker、SQLite、商品图和视频缓存位于同一主机。浏览器刷新或服务重启后仍可通过项目列表恢复工作，但数据不会跨机器同步。生产化时建议将 worker 替换为 Celery、RQ 或 Temporal，将 SQLite 换成 PostgreSQL、视频缓存换成对象存储，并为 H3 提交增加服务端幂等键；展示层与 Provider 接口可以继续沿用。
