"""
小说读取器
==========
读取 .txt 小说文件并按段落分块，供 LLM 逐块处理。

特性：
- 自动编码探测：依次尝试 UTF-8、GBK、GB18030，最终 fallback 替换
- 惰性读取：首次访问 full_text 时才读取文件
- 段落感知分块：在双换行（段落边界）处切分，保持段落完整

使用方式：
    reader = NovelReader("novel.txt")
    chunks = reader.chunks(3000)  # 每块约 3000 字符
"""

from pathlib import Path


class NovelReader:
    """读取纯文本小说，提供按字符数分块的能力。

    核心功能：
    - 惰性读取：首次访问 full_text 时才读文件，之后缓存
    - 自动编码探测：UTF-8 → GBK → GB18030 → 强制读取（errors=replace）
    - 按字符数分块：在双换行（段落边界）处切分，保持段落完整
    """

    def __init__(self, file_path: str | Path):  # file_path: 小说 .txt 文件路径
        self.file_path = Path(file_path)        # 小说文件的 Path 对象
        self._full_text: str | None = None      # 缓存全文，首次访问时惰性加载

    @property  # @property 让方法像属性一样访问：reader.full_text 而不是 reader.full_text()
    def full_text(self) -> str:
        """惰性读取全文：第一次访问时才读文件，之后返回缓存。"""
        if self._full_text is None:
            self._full_text = self._read_with_fallback()
        return self._full_text

    def _read_with_fallback(self) -> str:
        """编码探测策略：UTF-8 → GBK → GB18030 → 强制读取。

        探测流程：
        1. 依次尝试 ("utf-8", "gbk", "gb18030") 三种编码
        2. 每种编码调用 file_path.read_text(encoding=...) 尝试读取
        3. 如果抛出 UnicodeDecodeError → continue 试下一个
        4. 三种都失败 → 用 errors="replace" 强制读取（不可解码字符变为 �）
        """
        for encoding in ("utf-8", "gbk", "gb18030"):
            try:
                return self.file_path.read_text(encoding=encoding)
            except (UnicodeDecodeError, UnicodeError):
                continue  # 当前编码解码失败，试下一个
        # 所有编码都失败，用 replace 模式强行读取（不可解码字符变为 �）
        return self.file_path.read_text(encoding="utf-8", errors="replace")

    def chunks(self, size: int) -> list[str]:  # size: 每块目标字符数
        """按大约 size 字符将小说切分为多个块。

        切分算法（段落感知）：
        1. text.split("\n\n") 将全文拆成段落列表
        2. 逐段累积到 current 列表，同时累计 current_len
        3. 某段加入后 current_len > size 且 current 非空 → 把 current 拼成一个块，存入 chunks
        4. 该段作为新块的起始，重新开始累积
        5. 遍历结束后，把剩余的 current 拼成最后一个块

        每个块用 "\n\n".join(current) 重新拼接，保持段落格式。
        """
        text = self.full_text
        if len(text) <= size:
            return [text]  # 文本比 size 还短，不需要切分

        paragraphs = text.split("\n\n")  # 按双换行拆成段落
        chunks: list[str] = []
        current: list[str] = []  # 当前正在累积的段落
        current_len = 0  # 当前块的累计字符数

        for para in paragraphs:
            # 加入当前段落后会超限，且已有内容 → 先把已有内容存为一个块
            if current_len + len(para) > size and current:
                chunks.append("\n\n".join(current))  # 用双换行重新拼接，保持段落格式
                current = [para]  # 新块从当前段落开始
                current_len = len(para)
            else:
                current.append(para)
                current_len += len(para)

        if current:  # 别忘了最后一段不足 size 的剩余内容
            chunks.append("\n\n".join(current))

        return chunks

    def chunks_with_overlap(self, size: int, overlap: int) -> list[str]:  # size: 每块目标字符数, overlap: 相邻块重叠字符数
        """按大约 size 字符分块，相邻块之间有 overlap 字符的重叠。

        实现步骤：
        1. 调用 chunks(size) 得到不重叠的基础块列表 base_chunks
        2. 第一块不需要重叠，直接加入结果
        3. 从第二块开始，取上一块末尾的 overlap 个字符作为重叠前缀
        4. 重叠文本对齐到最近的段落边界（"\n\n"），避免在段落中间截断
           - 找到 overlap_text 中的 "\n\n" 位置，从下一段开始作为重叠
        5. 拼接：overlap_text + "\n\n" + base_chunks[i]

        效果：每块的开头都能看到上一块的结尾，保持叙事连贯。
        """
        base_chunks = self.chunks(size)
        if overlap <= 0 or len(base_chunks) <= 1:
            return base_chunks  # 不需要重叠，直接返回

        result: list[str] = [base_chunks[0]]  # 第一块不需要重叠
        for i in range(1, len(base_chunks)):
            prev = base_chunks[i - 1]
            if len(prev) > overlap:
                overlap_text = prev[-overlap:]  # 取上一块末尾的 overlap 字符
                # 对齐到最近的段落边界，避免在段落中间截断
                nl_pos = overlap_text.find("\n\n")
                if nl_pos != -1:
                    overlap_text = overlap_text[nl_pos + 2:]  # 跳过 \\n\\n，从下一段开始
                result.append(overlap_text + "\n\n" + base_chunks[i])
            else:
                # 上一块比 overlap 还短，直接把整块作为重叠
                result.append(prev + "\n\n" + base_chunks[i])

        return result

    @property
    def char_count(self) -> int:
        """小说总字符数。"""
        return len(self.full_text)
