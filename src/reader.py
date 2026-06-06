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
    """读取纯文本小说，提供按字符数分块的能力。"""

    def __init__(self, file_path: str | Path):
        self.file_path = Path(file_path)
        self._full_text: str | None = None

    @property
    def full_text(self) -> str:
        """惰性读取全文，自动探测编码（UTF-8 → GBK → GB18030）。"""
        if self._full_text is None:
            self._full_text = self._read_with_fallback()
        return self._full_text

    def _read_with_fallback(self) -> str:
        """依次尝试常见中文编码，读取成功则返回。"""
        for encoding in ("utf-8", "gbk", "gb18030"):
            try:
                return self.file_path.read_text(encoding=encoding)
            except (UnicodeDecodeError, UnicodeError):
                continue
        # 最终回退：用 errors="replace" 强行读取，替换不可解码字符
        return self.file_path.read_text(encoding="utf-8", errors="replace")

    def chunks(self, size: int) -> list[str]:
        """按大约 size 字符将小说切分为多个块。

        尽量在段落边界（双换行）处切分，保持段落完整。
        """
        text = self.full_text
        if len(text) <= size:
            return [text]

        paragraphs = text.split("\n\n")
        chunks: list[str] = []
        current: list[str] = []
        current_len = 0

        for para in paragraphs:
            if current_len + len(para) > size and current:
                chunks.append("\n\n".join(current))
                current = [para]
                current_len = len(para)
            else:
                current.append(para)
                current_len += len(para)

        if current:
            chunks.append("\n\n".join(current))

        return chunks

    def chunks_with_overlap(self, size: int, overlap: int) -> list[str]:
        """按大约 size 字符分块，相邻块之间有 overlap 字符的重叠。

        重叠区域从上一块的末尾取，对齐到最近的段落边界（\\n\\n）。
        当 overlap=0 时行为等同于 chunks(size)。
        """
        base_chunks = self.chunks(size)
        if overlap <= 0 or len(base_chunks) <= 1:
            return base_chunks

        result: list[str] = [base_chunks[0]]
        for i in range(1, len(base_chunks)):
            prev = base_chunks[i - 1]
            if len(prev) > overlap:
                overlap_text = prev[-overlap:]
                # 对齐到最近的段落边界，避免在段落中间截断
                nl_pos = overlap_text.find("\n\n")
                if nl_pos != -1:
                    overlap_text = overlap_text[nl_pos + 2:]
                result.append(overlap_text + "\n\n" + base_chunks[i])
            else:
                result.append(prev + "\n\n" + base_chunks[i])

        return result

    @property
    def char_count(self) -> int:
        """小说总字符数。"""
        return len(self.full_text)
