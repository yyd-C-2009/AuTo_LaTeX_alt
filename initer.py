from sentence_transformers import SentenceTransformer
import chromadb
import os
from pydantic import BaseModel, Field, create_model
from pydantic.fields import FieldInfo
from typing import Optional,Annotated,get_type_hints,get_origin,get_args,Any,Pattern,Union
from annotated_types import Gt,Le,Ge,Lt,MaxLen,MinLen,MultipleOf
from pydantic_core import PydanticUndefined
from functools import wraps
from enum import Enum
from Tools import Tools
from Saver import Saver
from Visal import Visal
import hashlib
import asyncio



async def init():
    agent_memory = await asyncio.to_thread(Saver)
    agent_visal = await asyncio.to_thread(Visal)
    # 测试加载是否报错，以及能否正常编码
    # test_model = SentenceTransformer("C:/Users/yangyiding/.cache/huggingface/hub/models--BAAI--bge-base-zh-v1.5/snapshots/f03589ceff5aac7111bd60cfc7d497ca17ecac65", local_files_only=True)
    # test_vector = test_model.encode("测试文本", normalize_embeddings=True)
    # print(f"向量维度：{len(test_vector)},前5位:{test_vector[:5].tolist()}")
    return {"agent_memory":agent_memory,'agent_visal':agent_visal}