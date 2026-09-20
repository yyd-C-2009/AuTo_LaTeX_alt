import hashlib
import os
import chromadb
from sentence_transformers import SentenceTransformer
from config import require


class Saver:
    def __init__(self):
        self.embed_model = self.load_embedder()
        chroma_client = chromadb.PersistentClient(path=require("paths.memory_database"))
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
        # 查找顺序：显式参数 > settings.json 的项目内模型目录。
        candidates: list[str] = []
        if local_dir:
            candidates.append(local_dir)
        candidates.append(os.path.join(os.path.dirname(os.path.abspath(__file__)), require("paths.embedding_model")))
        for directory in candidates:
            if directory and os.path.isdir(directory):
                # 强制 local_files_only=True，彻底规避防火墙
                return SentenceTransformer(directory, local_files_only=True)
        raise RuntimeError(
            "BGE 嵌入模型目录缺失。请先运行 python Setup.py 下载模型，"
            "或手动执行：modelscope download --model AI-ModelScope/bge-base-zh-v1.5 "
            "--local_dir ./models/bge-base-zh-v1.5（也可用 HF 镜像下载 BAAI/bge-base-zh-v1.5）。"
            "请在 settings.json 的 paths.embedding_model 中指定本地模型目录。"
        )



    def embed_text(self,text: str) -> list:
        # 归一化保证余弦相似度计算稳定
        return self.embed_model.encode(text, normalize_embeddings=True).tolist()

    # ---------- 4. 写入记忆（确定性ID防止重复脏数据） ----------
    def add_memory(self,text: str, metadata: dict = None):
        '''写入记忆，使用前必须先查询是否已存在相似记忆，否则会被拦截'''
        doc_id = self.get_stable_id(text)  # 确定性ID
        # 同一文本（相同MD5 ID）重复写入时用 upsert 覆盖，避免 ChromaDB DuplicateIDError
        self.collection.upsert(
            ids=[doc_id],
            documents=[text],
            embeddings=[self.embed_text(text)],
            metadatas=[metadata or {"source": "user_input"}]
        )
        print(f"已记忆 (ID: {doc_id[:8]}): {text[:20]}...")

    # ---------- 5. 删除/替换（仅按 ID，避免 Agent 幻觉按文本删除相似内容） ----------
    def get_memory_by_id(self, memory_id: str) -> dict | None:
        """按 ID 查询记忆，返回 {id,text,metadata} 或 None。"""
        if not memory_id:
            return None
        try:
            data = self.collection.get(ids=[memory_id], include=["documents", "metadatas"])
        except Exception as e:
            print(f"按 ID 查询失败：{type(e).__name__}: {e}")
            return None
        ids = data.get("ids") or []
        if not ids:
            return None
        docs = data.get("documents") or []
        metas = data.get("metadatas") or []
        return {
            "id": ids[0],
            "text": docs[0] if docs else "",
            "metadata": metas[0] if metas else None,
        }

    def delete_memory(self, memory_id: str, exact_text: str) -> str:
        """按 ID + 精确文本 删除记忆。必须同时提供精确 ID 与完全一致的文本，避免误删相似内容。"""
        old = self.get_memory_by_id(memory_id)
        if old is None:
            return f"删除失败：ID {memory_id} 不存在。"
        if old.get("text", "") != exact_text:
            return (
                "删除失败：文本不匹配。必须同时提供精确 ID 与完全一致的文本才能删除；"
                f"库中该 ID 对应文本为：{old.get('text', '')[:80]}"
            )
        self.collection.delete(ids=[memory_id])
        return f"已删除记忆（ID: {memory_id[:8]}）：{old['text'][:50]}"

    def replace_memory(self, memory_id: str, old_text: str, new_text: str, metadata: dict = None) -> str:
        """按 ID + 旧文本精确匹配后替换：删除旧 ID，以新文本生成新 ID 写入（保持 ID=MD5(text)）。"""
        old = self.get_memory_by_id(memory_id)
        if old is None:
            return f"替换失败：ID {memory_id} 不存在。"
        if old.get("text", "") != old_text:
            return (
                "替换失败：旧文本不匹配。必须同时提供精确 ID 与完全一致的旧文本才能替换；"
                f"库中该 ID 对应文本为：{old.get('text', '')[:80]}"
            )
        if not new_text or not new_text.strip():
            return "替换失败：新文本不能为空。"
        new_text = new_text.strip()
        self.collection.delete(ids=[memory_id])
        new_id = self.get_stable_id(new_text)
        self.collection.upsert(
            ids=[new_id],
            documents=[new_text],
            embeddings=[self.embed_text(new_text)],
            metadatas=[metadata or old.get("metadata") or {"source": "user_input"}],
        )
        return f"已替换记忆：旧 ID {memory_id[:8]} -> 新 ID {new_id[:8]}；新文本：{new_text[:50]}"

    # ---------- 6. 检索（保留原逻辑，但确保注入时去重） ----------
    def retrieve_context(self,query: str, top_k: int = 3) -> str:
        '''查询历史记忆，如果对命令有任何不理解或者对要求有任何疑问，请先查阅记忆'''
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

    # ---------- 7. 查看全部记忆（terminal.py 的 /db 命令使用，不注册为 Agent 工具） ----------
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
