"""Shared embedder — the ONE embedding model every vector-based system uses.

Precomputes, once, the session-document matrix and question vector for every
item, so VectorRetriever / ReflexionRetriever (and, conceptually, Mem0, which
uses the same model id inside its own pipeline) all rank over identical vectors.
"""
from __future__ import annotations

import numpy as np

from harness import EMBED_MODEL, session_texts


class Embedder:
    def __init__(self, model_name: str = EMBED_MODEL, batch_size: int = 256):
        from sentence_transformers import SentenceTransformer
        self.model = SentenceTransformer(model_name)
        self.batch_size = batch_size
        self.doc_matrices: list[np.ndarray] = []
        self.q_vectors: list[np.ndarray] = []

    def encode_query(self, text: str) -> np.ndarray:
        return self.model.encode([text], normalize_embeddings=True, show_progress_bar=False)[0]

    def prepare(self, items: list[dict]) -> None:
        # Flatten every session across every item, encode in one batched pass, slice back.
        flat: list[str] = []
        spans: list[tuple[int, int]] = []
        questions: list[str] = []
        for item in items:
            texts, _ = session_texts(item)
            start = len(flat)
            flat.extend(texts)
            spans.append((start, len(flat)))
            questions.append(str(item.get("question", "")))
        doc_emb = self.model.encode(
            flat, normalize_embeddings=True, batch_size=self.batch_size, show_progress_bar=True
        )
        doc_emb = np.asarray(doc_emb, dtype=np.float32)
        q_emb = np.asarray(
            self.model.encode(questions, normalize_embeddings=True, batch_size=self.batch_size, show_progress_bar=True),
            dtype=np.float32,
        )
        self.doc_matrices = [doc_emb[a:b] for (a, b) in spans]
        self.q_vectors = [q_emb[i] for i in range(len(items))]
