# Novel2Script

将小说文本转换为结构化剧本，基于 LLM（OpenAI 兼容 API）。提供 Web 界面（ChatGPT 风格对话 + 文件上传）和命令行两种使用方式。

**Demo 视频**：[Bilibili 演示](https://www.bilibili.com/video/BV12yEb66EJX/)

## 快速开始

```bash
# 1. 安装依赖
pip install -r requirements.txt

# 2. 创建 .env 并填入 API 凭据
cp .env.example .env
# 编辑 .env：写入你的 OPENAI_BASE_URL 和 OPENAI_API_KEY

# 3. 编辑 config.py，填写模型名
# MODEL = "your-model-name"

# 4. 启动 Web 服务
python server.py
```

打开浏览器访问 **http://localhost:8000** 即可使用。

## 使用方式

### Web 界面

启动 `python server.py` 后，浏览器打开 `http://localhost:8000`。

**两种交互模式**：

| 模式 | 操作 | 说明 |
|------|------|------|
| 小说转剧本 | 点击 📎 选择 .txt 文件 → 发送 | 上传小说，AI 生成结构化剧本 |
| 常规对话 | 直接输入文字 → 发送 | 与 AI 讨论剧本修改、追问细节 |

两种模式共享同一个聊天界面和对话历史。生成剧本后继续输入「把第 3 场的对白改激烈一点」等追问，AI 会基于剧本上下文回答。

**API 设置**：点击标题栏右侧齿轮图标 ⚙ ，可填入自己的 Base URL、API Key 和 Model 名。设置保存在浏览器会话中，关闭标签页后需重新输入。

### 命令行

```bash
python main.py data/sample_novel.txt
```

结果输出到 `output/{小说名}/{小说名}.yaml`。

## HTTP API

| 端点 | 方法 | 说明 |
|------|------|------|
| `/api/v1/health` | GET | 健康检查 |
| `/api/v1/convert` | POST | 同步转换，返回完整剧本 JSON |
| `/api/v1/convert/stream` | POST | SSE 流式转换，实时推送进度和结果 |
| `/api/v1/convert/async` | POST | 异步转换（需 Redis），返回 task_id |
| `/api/v1/tasks/{task_id}` | GET | 查询异步任务状态和结果 |
| `/api/v1/download/{novel_name}` | GET | 下载已生成的 YAML 剧本文件 |
| `/api/v1/chat` | POST | 发送对话消息，返回 AI 回复 |
| `/api/v1/schema` | GET | 返回剧本 JSON Schema |
| `/ws` | WS | WebSocket 双向通信（聊天流式 + 任务进度） |

`/api/v1/convert`、`/api/v1/convert/stream`、`/api/v1/convert/async` 和 `/api/v1/chat` 均支持可选的 `model`、`base_url`、`api_key` 参数，用于运行时覆盖默认配置。

```bash
# 命令行调用示例
curl -X POST http://localhost:8000/api/v1/convert \
  -F "file=@data/sample_novel.txt" \
  -F "model=your-model-name"

curl -X POST http://localhost:8000/api/v1/chat \
  -H "Content-Type: application/json" \
  -d '{"messages":[{"role":"user","content":"你好"}],"model":"your-model-name"}'
```

## 多 Agent 架构

系统不是用一个 LLM 一口气完成转换，而是模拟真实编剧团队，拆分为六个专业 Agent 协作完成。

### 流程总览

```
小说 .txt
    │
    ▼
 Phase 1: 导演预读（并行分析全文，提取角色 + 剧情）
    │
    ▼
 Phase 2: 滑动窗口分块，逐块处理 ─────────────────────────┐
    │                                                      │
    ▼                                                      │
 编排师 → 划分场景 + 段落分类                               │
    │                                                      │
    ├── 对白专家 ──┐                                       │
    │   (并行)     ├──→ 合并 → 审查循环(最多3轮) → 记忆更新 │
    └── 动作专家 ──┘                                       │
                                                           │
    ◄── 编排预取：下一块的编排与当前块的专家/审查并行执行 ───┘
    │
    ▼
 合并所有块 → 输出 {小说名}.yaml
```

### 六个 Agent 的职责

| Agent | 阶段 | 输入 | 输出 | 职责 |
|-------|------|------|------|------|
| **导演** (Director) | Phase 1 | 小说片段 | 角色列表 + 剧情摘要 | 全局预读，建立初始角色档案和事件记忆 |
| **编排师** (Orchestrator) | Phase 2 | 小说片段 + 角色表 | 场景结构 + 段落分类 | 分析场景边界，标记每个段落为"对白"或"动作" |
| **对白专家** (Dialogue) | Phase 2 | 对白段落 + 角色表 | 对白 items | 提取并优化角色台词，保持人物性格一致 |
| **动作专家** (Action) | Phase 2 | 动作段落 + 角色表 | 动作 items | 将小说描写转换为剧本舞台指示 |
| **审查员** (Reviewer) | Phase 2 | 剧本片段 + 原文 | 审查结果 | 检查编号连续性、角色名一致性、剧情逻辑 |
| **记忆专家** (Memory) | Phase 2 | 最终剧本 + 角色表 | 更新后的角色表 + 事件摘要 | 跨片段传递叙事上下文 |

### 跨片段记忆机制

长篇小说分块处理后，最大的挑战是**上下文断裂**——前半段出场的角色在后半段可能消失，前面发生的情节在后面可能被遗忘。

系统通过两个共享状态解决这个问题：

- **角色注册表** (`character_registry`)：`dict[str, {description, is_main}]`，累积记录所有角色信息。每块处理后由记忆专家更新，次要角色超过 40 个时自动压缩描述。
- **事件记忆** (`event_summaries`)：`list[str]`，累积记录剧情事件。每块后自动压缩——保留最近的摘要，丢弃最早的，确保 prompt 长度始终在 `EVENT_MEMORY_MAX_CHARS`（默认 1500 字符）以内。

这两个状态在每块处理时注入到所有 Agent 的 prompt 中，让每个 Agent 都能"记住"前面发生了什么。

### 编排预取流水线

逐块串行处理会浪费大量等待时间。系统采用**预取策略**：

```python
# 当前块的编排结果已预取，直接使用
outline = prefetch_future.result()

# 与当前块的专家/审查并行，预取下一块的编排
if i + 1 < total_chunks:
    prefetch_future = _submit_orchestrator(chunks[i + 1], ...)

# 当前块的对白专家和动作专家也并行执行
f_dialogue = chunk_executor.submit(call_dialogue)
f_action = chunk_executor.submit(call_action)
```

每块的实际执行顺序：

```
编排（预取） → 对白专家 ∥ 动作专家 → 合并 → 审查循环 → 记忆更新
                                  ↑ 下一块编排与此并行
```

### 多轮审查闭环

每块生成后进入审查循环，最多 `REVIEW_MAX_ROUNDS`（默认 3）轮：

1. **审查员**检查剧本的场景编号、角色名一致性、剧情逻辑
2. 如果发现问题，**修正 Agent** 根据审查意见修改剧本（同时参考原始小说文本和角色注册表）
3. 修正后重新审查，直到通过或达到最大轮次
4. 如果所有轮次均未通过，**回滚到审查前的原始版本**，避免产出质量更差的结果

## 项目结构

```
├── .env                      # API 凭据（敏感，已 gitignore）
├── .gitignore
├── config.py                 # 运行参数（model, temperature 等）
├── server.py                 # FastAPI HTTP + WebSocket 服务
├── main.py                   # 命令行入口
├── requirements.txt
├── data/                     # 测试数据（已 gitignore）
│   └── sample_novel.txt
├── docs/
│   └── yaml_schema.md        # Schema 设计文档
├── output/                   # 生成结果
│   └── {小说名}/              # 每本小说独立目录
│       ├── {小说名}.yaml      # 校验后的剧本
│       ├── raw_chunk_*.txt   # 各块 LLM 原始输出（调试用）
│       └── director_chunk_*.txt  # 导演预读输出（调试用）
├── static/                   # 前端资源
│   ├── index.html            # 主页面
│   ├── script-viewer.html    # 剧本查看器弹窗
│   ├── style.css
│   └── app.js
└── src/
    ├── __init__.py
    ├── config.py             # 配置加载（.env + config.py）
    ├── reader.py             # 小说读取与分块（自动探测编码）
    ├── prompt.py             # 多 Agent Prompt 构造
    ├── llm_client.py         # LLM API 调用（含重试）
    ├── parser.py             # YAML 解析与 Pydantic 校验
    ├── schema_gen.py         # Schema 文档生成
    ├── pipeline.py           # 流程编排（多 Agent 协作）
    └── ws_manager.py         # WebSocket 连接管理
```

## 配置说明

三层配置，按优先级从高到低：

| 优先级 | 位置 | 存什么 | 提交 git |
|:---:|------|------|:---:|
| 1 | Web 界面齿轮 ⚙ | 运行时覆盖（存浏览器会话） | — |
| 2 | `.env` | API 凭据：`OPENAI_API_KEY`、`OPENAI_BASE_URL` | 忽略 |
| 3 | `config.py` | 运行参数：`MODEL`、`TEMPERATURE`、`CHUNK_SIZE` 等 | 提交 |

```bash
# .env 示例
OPENAI_API_KEY=sk-your-key-here
OPENAI_BASE_URL=https://api.openai.com/v1
```

```python
# config.py 示例
MODEL = "gpt-4o"
TEMPERATURE = 0.7
MAX_TOKENS = 4096
CHUNK_SIZE = 3000
OUTPUT_DIR = "./output"
EVENT_MEMORY_MAX_CHARS = 1500  # 事件记忆最大字符数，超过则自动压缩
DIRECTOR_ENABLED = True        # 是否启用导演 Agent 全局预读
DIRECTOR_CHUNK_SIZE = 15000    # 导演预读的分块大小
REVIEW_MAX_ROUNDS = 3          # 审查最大轮次
```

Web 界面中填入的值优先于 `.env` 和 `config.py`；未填的字段回退到下一层默认值。

## 输出剧本结构

```yaml
title: "剧本标题"
scenes:
  - scene_number: 1
    slugline: "内. 古宅门厅 - 夜"
    content:                      # 动作与对白按演出顺序交错排列
      - type: "action"
        text: "林侦探推开吱呀作响的橡木门。"
      - type: "dialogue"
        character: "林侦探"
        parenthetical: "警惕地"
        line: "有人在吗？"
characters:
  - name: "林侦探"
    description: "一名谨慎冷静的侦探"
```

`content` 列表中的顺序就是演出顺序，保留小说中动作与对白的交错节奏。设计原因详见 [docs/yaml_schema.md](docs/yaml_schema.md)。

## 依赖与原创说明

### 第三方依赖

| 库 | 版本要求 | 用途 |
|---|---|---|
| [openai](https://github.com/openai/openai-python) | >=1.0.0 | LLM API 客户端，调用 OpenAI 兼容接口 |
| [pydantic](https://github.com/pydantic/pydantic) | >=2.0.0 | 数据模型定义与校验（Script、Scene、CharacterInfo 等） |
| [PyYAML](https://github.com/yaml/pyyaml) | >=6.0 | YAML 解析与序列化 |
| [FastAPI](https://github.com/tiangolo/fastapi) | >=0.100.0 | Web 框架（HTTP REST + WebSocket） |
| [uvicorn](https://github.com/encode/uvicorn) | >=0.20.0 | ASGI 服务器 |
| [python-multipart](https://github.com/andrew-d/python-multipart) | >=0.0.6 | 文件上传解析 |
| [python-dotenv](https://github.com/theskumar/python-dotenv) | >=1.0.0 | 加载 .env 环境变量 |
| [redis](https://github.com/redis/redis-py) | >=5.0.0 | 可选，异步任务进度推送与状态存储 |
| [websockets](https://github.com/python-websockets/websockets) | >=12.0 | WebSocket 协议支持（FastAPI/Starlette 依赖） |

### 原创部分

以下模块和设计为项目原创实现：

- **多 Agent 流程编排** (`src/pipeline.py`) — 六个 Agent 的协作流程，包括导演预读、编排预取流水线、对白/动作专家并行执行、多轮审查闭环与回滚机制
- **Prompt 工程** (`src/prompt.py`) — 七个 Agent 的 system prompt 设计与构造函数，包含角色注册表注入、事件记忆传递、重叠区间跳过指令、场景边界约束等
- **跨片段记忆系统** (`src/pipeline.py`) — 角色注册表的累积更新与压缩策略、事件记忆的滑动窗口压缩、记忆专家的上下文提取
- **滑动窗口分块** (`src/reader.py`) — 按段落边界切分 + 按段落对齐的重叠区域拼接，保证不截断段落
- **Pydantic 校验体系** (`src/parser.py`) — 使用 `Discriminator` 的多态 ContentItem、七个 Agent 输出的解析函数、多块剧本的合并与去重
- **全栈 API 架构** (`server.py`) — 同步/SSE/异步三种转换通道、WebSocket 双向通信、Redis Pub/Sub 推送与内存降级
- **前端交互** (`static/app.js`) — 文件上传 + SSE 进度渲染、WebSocket 聊天流式输出、BroadcastChannel 跨窗口通信、剧本查看器

## 扩展方向

- **多格式输出** — 支持 Final Draft、Fountain 等剧本格式
- **Prompt 优化** — 调整 system prompt 提升生成质量
