# Novel2Script

将小说文本转换为结构化剧本，基于 LLM（OpenAI 兼容 API）。

## 快速开始

```bash
# 安装依赖
pip install -r requirements.txt

# 编辑 .env，填入 API 凭据
# OPENAI_API_KEY=sk-your-key-here
# OPENAI_BASE_URL=https://api.openai.com/v1

# 运行
python main.py data/sample_novel.txt
```

## 项目结构

```
├── .env                      # API 凭据（敏感，已 gitignore）
├── config.py                 # 运行参数（model, temperature 等）
├── main.py                   # 入口脚本
├── requirements.txt
├── data/                     # 小说输入
│   └── sample_novel.txt
├── output/                   # 生成结果
│   ├── {小说名}/              # 每本小说独立目录
│   │   ├── script.yaml       # 校验后的剧本
│   │   └── raw.txt           # LLM 原始输出（调试用）
│   └── schema/               # Schema 文档（共享）
│       ├── script_schema.json
│       └── script_schema.md
└── src/
    ├── config.py             # 配置加载（读取 .env + config.py）
    ├── reader.py             # 小说读取与分块
    ├── prompt.py             # Prompt 构造
    ├── llm_client.py         # LLM API 调用
    ├── parser.py             # YAML 解析与校验
    ├── schema_gen.py         # Schema 文档生成
    └── pipeline.py           # 流程编排
```

## 配置说明

**敏感凭据** → `.env`（不提交 git）

```bash
OPENAI_API_KEY=sk-your-key-here
OPENAI_BASE_URL=https://api.openai.com/v1
```

**运行参数** → `config.py`（可提交 git）

```python
MODEL = "gpt-4o"
TEMPERATURE = 0.7
MAX_TOKENS = 4096
CHUNK_SIZE = 3000
OUTPUT_DIR = "./output"
```

## 输出剧本结构

```yaml
title: "剧本标题"
acts:
  - act_number: 1
    scenes:
      - scene_number: 1
        setting: "场景描述"
        characters_present:
          - "角色A"
          - "角色B"
        dialogues:
          - character: "角色A"
            line: "台词"
            action: "动作/情绪提示"
        stage_directions: "舞台指示"
characters:
  - name: "角色A"
    description: "角色简介"
```

## 命令行参数

```
python main.py <小说路径>
```

| 参数 | 说明 |
|------|------|
| `novel_path` | 小说 .txt 文件路径 |

## 扩展方向

当前为初版骨架，后续可扩展：

- **多块合并** — 长篇小说分块处理后合并为完整剧本
- **角色一致性** — 跨章节保持角色名称和性格一致
- **Prompt 优化** — 调整 system prompt 提升生成质量
- **多格式输出** — 支持 Final Draft、Fountain 等剧本格式
- **多 LLM 适配** — 切换不同模型对比效果
