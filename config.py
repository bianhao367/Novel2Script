"""项目参数配置 —— 非敏感参数集中管理，敏感凭据请写在 .env 文件中。"""

# --- LLM 参数 ---
MODEL = "gpt-4o"
TEMPERATURE = 0.7
MAX_TOKENS = 4096

# --- 流程参数 ---
CHUNK_SIZE = 3000          # 每次发给 LLM 的最大字符数
OUTPUT_DIR = "./output"    # 输出根目录
OUTPUT_FORMAT = "yaml"     # 输出格式
