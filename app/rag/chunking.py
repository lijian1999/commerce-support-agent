"""Markdown 知识库的切块：按文档标题 + `## ` 小节切，超长小节按段落再拆。"""
import hashlib
from dataclasses import dataclass
from pathlib import PurePosixPath

from app import config


@dataclass
class Chunk:
    doc: str            # 相对 data/documents 的路径，如 policies/return_policy.md
    section: str        # 小节标题
    text: str           # 送去生成 Embedding 的文本（含文档标题和小节标题）
    content_hash: str   # sha256_text(text)


def normalize(text: str) -> str:
    """统一换行为 \\n，去掉每行行尾空白和整段首尾的空行，结果不以换行符结尾。"""
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    lines = [line.rstrip() for line in text.split("\n")]
    start, end = 0, len(lines)
    while start < end and not lines[start]:
        start += 1
    while end > start and not lines[end - 1]:
        end -= 1
    return "\n".join(lines[start:end])


def sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _split_blocks(body: str, max_chars: int) -> list[str]:
    """正文按段落（空行分隔）从前往后贪心合并，单段超过 max_chars 时自成一块。"""
    if len(body) <= max_chars:
        return [body]
    blocks: list[str] = []
    current: list[str] = []
    for para in body.split("\n\n"):
        if not current:
            current = [para]
        elif len("\n\n".join(current + [para])) <= max_chars:
            current.append(para)
        else:
            blocks.append("\n\n".join(current))
            current = [para]
    if current:
        blocks.append("\n\n".join(current))
    return blocks


def chunk_markdown(doc: str, text: str, max_chars: int = config.CHUNK_MAX_CHARS) -> list[Chunk]:
    """把一篇 Markdown 切成片段，顺序和文档里出现的顺序一致，片段之间不重叠。"""
    lines = normalize(text).split("\n")
    title_index = next((i for i, line in enumerate(lines) if line.startswith("# ")), None)
    if title_index is None:
        title = PurePosixPath(doc).stem
    else:
        title = lines[title_index][2:].strip()

    # 以 `## ` 行划分小节；`### ` 及更深的标题留在所属小节的正文里。
    sections: list[tuple[str, list[str]]] = []
    section = "Introduction"
    body: list[str] = []
    for i, line in enumerate(lines):
        if i == title_index:
            continue
        if line.startswith("## "):
            sections.append((section, body))
            section = line[3:].strip()
            body = []
        else:
            body.append(line)
    sections.append((section, body))

    chunks: list[Chunk] = []
    for sec, sec_lines in sections:
        content = "\n".join(sec_lines).strip()
        if not content:
            continue
        for block in _split_blocks(content, max_chars):
            chunk_text = f"{title}\n{sec}\n\n{block}"
            chunks.append(Chunk(doc=doc, section=sec, text=chunk_text, content_hash=sha256_text(chunk_text)))
    return chunks
