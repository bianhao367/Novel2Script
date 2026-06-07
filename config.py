"""项目参数配置 —— 非敏感参数集中管理，敏感凭据请写在 .env 文件中。"""

# --- LLM 参数 ---
MODEL = "ep-20260312161409-csvf6"
TEMPERATURE = 0.7
MAX_TOKENS = 4096

# --- 流程参数 ---
CHUNK_SIZE = 3000              # 每次发给 LLM 的最大字符数
OUTPUT_DIR = "./output"        # 输出根目录
OVERLAP_SIZE = 500             # 滑动窗口重叠字符数
EVENT_MEMORY_MAX_CHARS = 1500  # 事件记忆最大字符数，超过则自动压缩

# --- 导演 Agent ---
DIRECTOR_ENABLED = True        # 是否启用导演 Agent 全局预读
DIRECTOR_CHUNK_SIZE = 15000    # 导演预读的分块大小（够大以减少调用次数，导演任务简单不需要太小）

# --- 审查闭环 ---
REVIEW_MAX_ROUNDS = 3          # 审查最大轮次（1=单次审查+单次修正，兼容旧行为）
