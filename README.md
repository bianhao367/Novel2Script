# Novel2Script

将小说文本转换为结构化剧本，基于 LLM（OpenAI 兼容 API）。

## 快速开始

```bash
# 安装依赖
pip install -r requirements.txt

# 设置 API Key
export OPENAI_API_KEY="sk-your-key-here"

# 运行
python main.py data/sample_novel.txt
```

## 项目结构

```
├── .config                  # API 配置文件（含 key，已 gitignore）
├── main.py                  # 入口脚本
├── requirements.txt
├── data/                    # 小说输入
│   └── sample_novel.txt
├── output/                  # 生成结果
│   ├── {小说名}/             # 每本小说独立目录
│   │   ├── script.yaml      # 校验后的剧本
│   │   └── raw.txt          # LLM 原始输出（调试用）
│   └── schema/              # Schema 文档（共享）
│       ├── script_schema.json
│       └── script_schema.md
└── src/
    ├── config.py            # 配置加载
    ├── reader.py            # 小说读取与分块
    ├── prompt.py            # Prompt 构造
    ├── llm_client.py        # LLM API 调用
    ├── parser.py            # YAML 解析与校验
    ├── schema_gen.py        # Schema 文档生成
    └── pipeline.py          # 流程编排
```

## 配置文件

编辑 `.config`：

```ini
[api]
base_url = https://api.openai.com/v1
api_key = ${OPENAI_API_KEY}
model = gpt-4o
temperature = 0.7
max_tokens = 4096

[pipeline]
chunk_size = 3000
output_dir = ./output
output_format = yaml
```

- `api_key` 支持 `${ENV_VAR}` 语法从环境变量读取
- 兼容所有 OpenAI 格式 API（修改 `base_url` 和 `model` 即可切换）

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
python main.py <小说路径> [--config .config]
```

| 参数 | 说明 |
|------|------|
| `novel_path` | 小说 .txt 文件路径 |
| `--config`, `-c` | 配置文件路径（默认 `.config`） |

## 扩展方向

当前为初版骨架，后续可扩展：

- **多块合并** — 长篇小说分块处理后合并为完整剧本
- **角色一致性** — 跨章节保持角色名称和性格一致
- **Prompt 优化** — 调整 system prompt 提升生成质量
- **多格式输出** — 支持 Final Draft、Fountain 等剧本格式
- **多 LLM 适配** — 切换不同模型对比效果
