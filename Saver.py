import hashlib
import os
import chromadb
from sentence_transformers import SentenceTransformer


class Saver:
    def __init__(self):
        self.embed_model = self.load_embedder()
        chroma_client = chromadb.PersistentClient(path="./my_rag_db")
        self.collection = chroma_client.get_or_create_collection(
            name="agent_memory",
            metadata={"hnsw:space": "cosine"}
        )
        print("----------Saver loaded----------")

    # ---------- 1. 确定性ID生成器（替代hash） ----------
    def get_stable_id(self,text: str) -> str:
        """使用MD5保证跨会话、跨机器的一致ID"""
        return hashlib.md5(text.encode('utf-8')).hexdigest()

    # ---------- 2. 离线优先的嵌入模型加载 ----------
    def load_embedder(self,local_dir: str = "C:/Users/yangyiding/.cache/huggingface/hub/models--BAAI--bge-base-zh-v1.5/snapshots/f03589ceff5aac7111bd60cfc7d497ca17ecac65"):
        if not os.path.isdir(local_dir):
            raise RuntimeError(
                f"模型目录缺失。请离线下载至 {local_dir}。"
                "推荐命令（使用modelscope）："
                "modelscope download --model AI-ModelScope/bge-base-zh-v1.5 --local_dir ./models/bge-base-zh-v1.5"
            )
        # 强制 local_files_only=True，彻底规避防火墙
        return SentenceTransformer(local_dir, local_files_only=True)



    def embed_text(self,text: str) -> list:
        # 归一化保证余弦相似度计算稳定
        return self.embed_model.encode(text, normalize_embeddings=True).tolist()

    # ---------- 4. 写入记忆（确定性ID防止重复脏数据） ----------
    def add_memory(self,text: str, metadata: dict = None):
        '''写入记忆，使用前必须向user确认文本内容，避免误写'''
        doc_id = self.get_stable_id(text)  # 确定性ID
        # 若同一文本重复写入，ChromaDB会根据ID自动覆盖，避免冗余
        self.collection.add(
            ids=[doc_id],
            documents=[text],
            embeddings=[self.embed_text(text)],
            metadatas=[metadata or {"source": "user_input"}]
        )
        print(f"已记忆 (ID: {doc_id[:8]}): {text[:20]}...")

    # ---------- 5. 检索（保留原逻辑，但确保注入时去重） ----------
    def retrieve_context(self,query: str, top_k: int = 3) -> str:
        '''查询历史记忆'''
        if self.collection.count() == 0:
            return 'No data'
        results = self.collection.query(
            query_embeddings=[self.embed_text(query)],
            n_results=top_k
        )
        if results['documents'] and results['documents'][0]:
            # 利用 set 去重（虽然ID已保证，但防御性编程）
            unique_docs = list(dict.fromkeys(results['documents'][0]))
            return "\n---\n".join(unique_docs)
        return "No data"