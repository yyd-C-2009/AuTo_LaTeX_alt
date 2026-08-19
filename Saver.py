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
    def load_embedder(self, local_dir: str | None = None):
        # 查找顺序：显式参数 > 环境变量 BGE_MODEL_DIR > 项目本地 ./models/bge-base-zh-v1.5 > 旧机器兼容路径。
        candidates: list[str] = []
        if local_dir:
            candidates.append(local_dir)
        env_dir = os.environ.get("BGE_MODEL_DIR")
        if env_dir:
            candidates.append(env_dir)
        candidates.append(
            os.path.join(os.path.dirname(os.path.abspath(__file__)), "models", "bge-base-zh-v1.5")
        )
        candidates.append(
            "C:/Users/yangyiding/.cache/huggingface/hub/models--BAAI--bge-base-zh-v1.5/snapshots/f03589ceff5aac7111bd60cfc7d497ca17ecac65"
        )
        for directory in candidates:
            if directory and os.path.isdir(directory):
                # 强制 local_files_only=True，彻底规避防火墙
                return SentenceTransformer(directory, local_files_only=True)
        raise RuntimeError(
            "BGE 嵌入模型目录缺失。请先运行 python Setup.py 下载模型，"
            "或手动执行：modelscope download --model AI-ModelScope/bge-base-zh-v1.5 "
            "--local_dir ./models/bge-base-zh-v1.5（也可用 HF 镜像下载 BAAI/bge-base-zh-v1.5）。"
            "下载后可通过环境变量 BGE_MODEL_DIR 指定目录；默认查找项目内 ./models/bge-base-zh-v1.5。"
        )



    def embed_text(self,text: str) -> list:
        # 归一化保证余弦相似度计算稳定
        return self.embed_model.encode(text, normalize_embeddings=True).tolist()

    # ---------- 4. 写入记忆（确定性ID防止重复脏数据） ----------
    def add_memory(self,text: str, metadata: dict = None):
        '''写入记忆，使用前必须向user确认文本内容，避免误写'''
        doc_id = self.get_stable_id(text)  # 确定性ID
        # 同一文本（相同MD5 ID）重复写入时用 upsert 覆盖，避免 ChromaDB DuplicateIDError
        self.collection.upsert(
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

    # ---------- 6. 查看全部记忆（terminal.py 的 /db 命令使用，不注册为 Agent 工具） ----------
    def list_memories(self) -> list:
        """列出数据库中的全部记忆，返回 [{"id":..., "text":..., "metadata":...}]。"""
        if self.collection.count() == 0:
            return []
        data = self.collection.get(include=["documents", "metadatas"])
        ids = data.get("ids") or []
        docs = data.get("documents") or []
        metas = data.get("metadatas") or []
        result = []
        for i, doc_id in enumerate(ids):
            result.append({
                "id": doc_id,
                "text": docs[i] if i < len(docs) else "",
                "metadata": metas[i] if i < len(metas) else None,
            })
        return result
