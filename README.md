# Novel2Script

将小说文本转换为结构化剧本，基于 LLM（OpenAI 兼容 API）。提供 Web 界面（ChatGPT 风格对话 + 文件上传）和命令行两种使用方式。

## 快速开始

```bash
# 1. 安装依赖
pip install -r requirements.txt

# 2. 编辑 .env，填入 API 凭据（可选，也可在 Web 界面中设置）
# OPENAI_API_KEY=sk-your-key-here
# OPENAI_BASE_URL=https://api.openai.com/v1

# 3. 编辑 config.py，填写模型名和运行参数
# MODEL = "gpt-4o"   ← 改成你实际使用的模型名

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

**API 设置**：点击标题栏右侧齿轮图标 ⚙ ，可填入自己的 Base URL、API Key 和 Model 名。设置保存在浏览器中，下次打开页面无需重新输入。

### 命令行

```bash
python main.py data/sample_novel.txt
```

结果输出到 `output/{小说名}/script.yaml`。

## HTTP API

| 端点 | 方法 | 说明 |
|------|------|------|
| `/api/v1/health` | GET | 健康检查 |
| `/api/v1/convert` | POST | 上传 .txt 文件，返回剧本 JSON |
| `/api/v1/chat` | POST | 发送对话消息，返回 AI 回复 |
| `/api/v1/schema` | GET | 返回剧本 JSON Schema |

`/api/v1/convert` 和 `/api/v1/chat` 均支持可选的 `model`、`base_url`、`api_key` 参数，用于运行时覆盖默认配置。

```bash
# 命令行调用示例
curl -X POST http://localhost:8000/api/v1/convert \
  -F "file=@data/sample_novel.txt" \
  -F "model=your-model-name"

curl -X POST http://localhost:8000/api/v1/chat \
  -H "Content-Type: application/json" \
  -d '{"messages":[{"role":"user","content":"你好"}],"model":"your-model-name"}'
```

## 项目结构

```
├── .env                      # API 凭据（敏感，已 gitignore）
├── .gitignore
├── config.py                 # 运行参数（model, temperature 等）
├── server.py                 # FastAPI HTTP 服务（含静态文件）
├── main.py                   # 命令行入口
├── requirements.txt
├── data/                     # 小说输入
│   └── sample_novel.txt
├── docs/
│   └── yaml_schema.md        # Schema 设计文档
├── output/                   # 生成结果
│   ├── {小说名}/              # 每本小说独立目录
│   │   ├── script.yaml       # 校验后的剧本
│   │   └── raw.txt           # LLM 原始输出（调试用）
│   └── schema/               # Schema 文档（全局共享）
├── static/                   # 前端资源
│   ├── index.html
│   ├── style.css
│   └── app.js
└── src/
    ├── config.py             # 配置加载（.env + config.py）
    ├── reader.py             # 小说读取与分块（自动探测编码）
    ├── prompt.py             # Prompt 构造
    ├── llm_client.py         # LLM API 调用（含重试）
    ├── parser.py             # YAML 解析与 Pydantic 校验
    ├── schema_gen.py         # Schema 文档生成
    └── pipeline.py           # 流程编排
```

## 配置说明

三层配置，按优先级从高到低：

| 优先级 | 位置 | 存什么 | 提交 git |
|:---:|------|------|:---:|
| 1 | Web 界面齿轮 ⚙ | 运行时覆盖（存浏览器 localStorage） | — |
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

## 扩展方向

- **多块合并** — 长篇小说分块处理后合并为完整剧本
- **角色一致性** — 跨章节保持角色名称和性格一致
- **Prompt 优化** — 调整 system prompt 提升生成质量
- **多格式输出** — 支持 Final Draft、Fountain 等剧本格式
- **流式输出** — LLM 响应实时推送到前端
