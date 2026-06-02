import os
import json
import asyncio
from typing import Dict, List, Tuple

import faiss
import numpy as np
from sentence_transformers import SentenceTransformer

from itte.config import settings
from itte.observability import logger, MEMORY_ITEMS_GAUGE

class VectorStore:
    """
    Persistent FAISS vector store.

    - 启动时 load existing .index
    - 若不存在，则从 DB memory_items 重建
    - 新 memory 使用 add_with_ids 增量加入
    - 定期后台 rebuild，避免索引和 DB 长期漂移
    """

    def __init__(self):
        self.index_dir = settings.index_dir
        self.index_path = os.path.join(self.index_dir, "memory.faiss.index")
        self.ids_path = os.path.join(self.index_dir, "memory.ids.json")

        self.model_name = settings.embed_model
        self.model = None

        self.index = None
        self.ids = set()
        self.lock = asyncio.Lock()

        os.makedirs(self.index_dir, exist_ok=True)

    def _get_model(self):
        if self.model is None:
            logger.info(f"loading_embedding_model model={self.model_name}")
            self.model = SentenceTransformer(self.model_name)
        return self.model

    def _embed(self, texts: List[str]) -> np.ndarray:
        model = self._get_model()
        vectors = model.encode(
            texts,
            normalize_embeddings=True,
            show_progress_bar=False,
        )
        return np.asarray(vectors, dtype="float32")

    def _new_index(self, dim: int):
        base = faiss.IndexFlatIP(dim)
        return faiss.IndexIDMap2(base)

    def _save(self):
        if self.index is not None:
            faiss.write_index(self.index, self.index_path)

        with open(self.ids_path, "w", encoding="utf-8") as f:
            json.dump(sorted(list(self.ids)), f)

        logger.info(
            f"faiss_index_saved path={self.index_path} ids={len(self.ids)}"
        )

    def _load_from_disk(self) -> bool:
        if not os.path.exists(self.index_path) or not os.path.exists(self.ids_path):
            return False

        try:
            self.index = faiss.read_index(self.index_path)

            with open(self.ids_path, "r", encoding="utf-8") as f:
                self.ids = set(json.load(f))

            MEMORY_ITEMS_GAUGE.set(len(self.ids))

            logger.info(
                f"faiss_index_loaded path={self.index_path} ids={len(self.ids)}"
            )

            return True

        except Exception as e:
            logger.exception(f"faiss_index_load_failed error={e}")
            self.index = None
            self.ids = set()
            return False

    async def initialize(self, memory_rows: List[Dict]):
        async with self.lock:
            loaded = self._load_from_disk()

            if not loaded:
                logger.info("faiss_index_not_found rebuilding_from_db=true")
                await self.rebuild(memory_rows)
                return

            missing = [r for r in memory_rows if int(r["id"]) not in self.ids]

            if missing:
                logger.info(f"faiss_index_missing_rows count={len(missing)}")
                await self.add_many(missing, save=True)

    async def rebuild(self, memory_rows: List[Dict]):
        async with self.lock:
            logger.info(f"faiss_rebuild_start rows={len(memory_rows)}")

            self.ids = set()

            if not memory_rows:
                dummy = self._embed(["dummy"])
                self.index = self._new_index(dummy.shape[1])
                self._save()
                MEMORY_ITEMS_GAUGE.set(0)
                logger.info("faiss_rebuild_empty_complete")
                return

            texts = [r["text"] for r in memory_rows]
            ids = np.asarray([int(r["id"]) for r in memory_rows], dtype="int64")
            vectors = self._embed(texts)

            self.index = self._new_index(vectors.shape[1])
            self.index.add_with_ids(vectors, ids)

            self.ids = set(int(x) for x in ids.tolist())

            self._save()
            MEMORY_ITEMS_GAUGE.set(len(self.ids))

            logger.info(f"faiss_rebuild_complete ids={len(self.ids)}")

    async def add_one(self, memory_id: int, text: str):
        async with self.lock:
            if memory_id in self.ids:
                logger.info(f"faiss_add_skipped_existing memory_id={memory_id}")
                return

            vector = self._embed([text])
            ids = np.asarray([int(memory_id)], dtype="int64")

            if self.index is None:
                self.index = self._new_index(vector.shape[1])

            self.index.add_with_ids(vector, ids)
            self.ids.add(int(memory_id))

            self._save()
            MEMORY_ITEMS_GAUGE.set(len(self.ids))

            logger.info(f"faiss_memory_added memory_id={memory_id}")

    async def add_many(self, memory_rows: List[Dict], save: bool = True):
        if not memory_rows:
            return

        rows = [r for r in memory_rows if int(r["id"]) not in self.ids]
        if not rows:
            return

        texts = [r["text"] for r in rows]
        ids = np.asarray([int(r["id"]) for r in rows], dtype="int64")
        vectors = self._embed(texts)

        if self.index is None:
            self.index = self._new_index(vectors.shape[1])

        self.index.add_with_ids(vectors, ids)

        for x in ids.tolist():
            self.ids.add(int(x))

        if save:
            self._save()

        MEMORY_ITEMS_GAUGE.set(len(self.ids))

        logger.info(f"faiss_memory_many_added count={len(rows)}")

    async def search(self, text: str, k: int = 5) -> List[Tuple[int, float]]:
        async with self.lock:
            if self.index is None or self.index.ntotal == 0:
                return []

            q = self._embed([text])
            scores, ids = self.index.search(q, k)

            result = []

            for memory_id, score in zip(ids[0], scores[0]):
                if memory_id < 0:
                    continue
                result.append((int(memory_id), float(score)))

            return result

async def periodic_rebuild_loop(vector_store: VectorStore, load_memory_rows, interval_seconds: int):
    while True:
        await asyncio.sleep(interval_seconds)

        try:
            rows = load_memory_rows()
            await vector_store.rebuild(rows)
            logger.info("periodic_faiss_rebuild_success")
        except Exception as e:
            logger.exception(f"periodic_faiss_rebuild_failed error={e}")
