"""Embedding 服务：OpenAI 兼容接口，每 32 条一批。"""
from langchain_openai import OpenAIEmbeddings

from app import config

_BATCH = 32


def get_embeddings() -> OpenAIEmbeddings:
    """构造 Embedding 客户端。

    check_embedding_ctx_length=False 不能省：省掉后会在本地分词并发送 token 数组，
    非 OpenAI 的服务不认。
    """
    return OpenAIEmbeddings(
        base_url=config.EMBED_BASE_URL,
        api_key="EMPTY",
        model=config.EMBED_MODEL,
        check_embedding_ctx_length=False,
    )


def embed_texts(texts: list[str]) -> list[list[float]]:
    """批量生成向量，结果按原顺序返回。"""
    if not texts:
        return []
    client = get_embeddings()
    vectors: list[list[float]] = []
    for start in range(0, len(texts), _BATCH):
        vectors.extend(client.embed_documents(texts[start:start + _BATCH]))
    return vectors


def embed_query(text: str) -> list[float]:
    return get_embeddings().embed_query(text)
