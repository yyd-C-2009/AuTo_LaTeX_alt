"""Saver.add_memory upsert 验证（需本机环境：chromadb + sentence_transformers + 本地 bge 模型）

验证点：同一文本（相同 MD5 ID）重复 add_memory，不应抛 DuplicateIDError，
      而应被 upsert 覆盖，最终 collection 里该 ID 只存在一条且内容为最新。

用法（本机，需已安装依赖并存在本地 bge 模型）：
    python test_saver_upsert_demo.py
"""


def main():
    from Saver import Saver
    import chromadb
    from sentence_transformers import SentenceTransformer

    local_dir = "C:/Users/yangyiding/.cache/huggingface/hub/models--BAAI--bge-base-zh-v1.5/snapshots/f03589ceff5aac7111bd60cfc7d497ca17ecac65"

    saver = Saver.__new__(Saver)
    saver.embed_model = SentenceTransformer(local_dir, local_files_only=True)

    client = chromadb.PersistentClient(path="./_test_rag_db")
    col = client.get_or_create_collection(
        name="test_agent_memory",
        metadata={"hnsw:space": "cosine"},
    )
    saver.collection = col

    text = "测试用记忆：勾股定理 a^2 + b^2 = c^2"

    # 第一次写入
    saver.add_memory(text, {"source": "test_1"})
    print("第一次写入 OK")

    # 第二次写入完全相同的文本（相同 ID）—— 旧版 add 会 DuplicateIDError，新版 upsert 应覆盖
    try:
        saver.add_memory(text, {"source": "test_2"})
        print("第二次写入（相同 ID）OK —— upsert 生效，未抛 DuplicateIDError")
    except Exception as e:
        print(f"❌ 第二次写入失败：{type(e).__name__}: {e}")
        raise SystemExit(1)

    # 验证库里该 ID 只有一条
    doc_id = saver.get_stable_id(text)
    got = col.get(ids=[doc_id])
    docs = got.get("documents") or []
    print(f"collection 中该 ID 的文档条数：{len(docs)}（应为 1）")
    assert len(docs) == 1, "重复写入应覆盖为一条，实际多于一条"

    metas = got.get("metadatas") or [None]
    print(f"最新 metadata：{metas[0]}（应为 test_2，证明覆盖生效）")
    assert metas[0] == {"source": "test_2"}, "metadata 应被覆盖为最新值"

    # 清理测试库
    col.delete(ids=[doc_id])
    print("\n===== Saver upsert 验证通过 =====")


if __name__ == "__main__":
    main()
