"""
V3 Evidence Retrieval with pgvector
Semantic search over evidence using embeddings
"""

import os

import httpx
import numpy as np
import structlog

from .db import Database
from .entities import EvidenceItem

log = structlog.get_logger()


class EvidenceRetrieval:
    """Semantic search over evidence using vector embeddings"""

    def __init__(self, db: Database):
        """
        Initialize retrieval system

        Args:
            db: Database instance
        """
        self.db = db
        self._embedding_method = None  # lazy init

    async def _get_embedding(self, text: str) -> list[float]:
        """
        Get embedding vector for text

        Tries OpenAI API first, falls back to random vectors

        Args:
            text: Text to embed

        Returns:
            1536-dimensional embedding vector
        """
        # Try OpenAI via Codex OAuth endpoint
        if self._embedding_method is None:
            self._embedding_method = await self._detect_embedding_method()

        if self._embedding_method == "openai":
            return await self._openai_embed(text)
        else:
            return self._random_embed(text)

    async def _detect_embedding_method(self) -> str:
        """Detect which embedding method to use"""

        # Check for OpenAI API key
        api_key = os.getenv("OPENAI_API_KEY")
        if not api_key:
            log.warning("no_openai_key_using_random_embeddings")
            return "random"

        # Try a test embedding
        try:
            async with httpx.AsyncClient() as client:
                response = await client.post(
                    "https://api.openai.com/v1/embeddings",
                    headers={
                        "Authorization": f"Bearer {api_key}",
                        "Content-Type": "application/json",
                    },
                    json={
                        "input": "test",
                        "model": "text-embedding-3-small",
                    },
                    timeout=10.0,
                )

                if response.status_code == 200:
                    log.info("openai_embeddings_available")
                    return "openai"
                else:
                    log.warning(
                        "openai_embeddings_failed",
                        status=response.status_code,
                        using_random=True
                    )
                    return "random"

        except Exception as e:
            log.warning("openai_embeddings_error", error=str(e), using_random=True)
            return "random"

    async def _openai_embed(self, text: str) -> list[float]:
        """Get embedding from OpenAI API"""
        api_key = os.getenv("OPENAI_API_KEY")

        async with httpx.AsyncClient() as client:
            response = await client.post(
                "https://api.openai.com/v1/embeddings",
                headers={
                    "Authorization": f"Bearer {api_key}",
                    "Content-Type": "application/json",
                },
                json={
                    "input": text[:8000],  # truncate long text
                    "model": "text-embedding-3-small",
                },
                timeout=30.0,
            )

            response.raise_for_status()
            data = response.json()
            embedding = data["data"][0]["embedding"]

            # Ensure it's 1536 dimensions
            if len(embedding) != 1536:
                # Pad or truncate to 1536
                if len(embedding) < 1536:
                    embedding = embedding + [0.0] * (1536 - len(embedding))
                else:
                    embedding = embedding[:1536]

            return embedding

    def _random_embed(self, text: str) -> list[float]:
        """
        Generate deterministic random embedding based on text hash

        TODO: Replace with real embeddings (sentence-transformers or OpenAI)

        Args:
            text: Text to embed

        Returns:
            1536-dimensional random vector
        """
        # Use text hash as seed for deterministic randomness
        seed = hash(text) % (2**32)
        rng = np.random.RandomState(seed)

        # Generate random vector and normalize
        vec = rng.randn(1536)
        vec = vec / np.linalg.norm(vec)

        return vec.tolist()

    async def search(
        self,
        condition_id: str,
        query: str,
        top_k: int = 10
    ) -> list[EvidenceItem]:
        """
        Semantic search for relevant evidence

        Args:
            condition_id: Condition to search evidence for
            query: Search query text
            top_k: Number of results to return

        Returns:
            List of most relevant EvidenceItems
        """
        # Get query embedding
        query_embedding = await self._get_embedding(query)
        embedding_str = f"[{','.join(map(str, query_embedding))}]"

        # Vector similarity search using pgvector
        sql = """
            SELECT
                evidence_id, condition_id, doc_id, ts_event, ts_observed,
                polarity, claim, reliability, freshness_hours, extracted_values,
                embedding, created_at,
                (embedding <=> $1::vector) as distance
            FROM evidence_items
            WHERE condition_id = $2
                AND embedding IS NOT NULL
            ORDER BY distance ASC
            LIMIT $3
        """

        rows = await self.db.fetch(sql, embedding_str, condition_id, top_k)

        items = []
        for row in rows:
            # Convert embedding from string to list
            embedding = None
            if row.get('embedding'):
                embedding_str_result = str(row['embedding'])
                if embedding_str_result.startswith('[') and embedding_str_result.endswith(']'):
                    embedding = [float(x) for x in embedding_str_result[1:-1].split(',')]

            # Parse extracted_values if it's a string
            import json
            extracted_values = row['extracted_values']
            if isinstance(extracted_values, str):
                extracted_values = json.loads(extracted_values)

            items.append(EvidenceItem(
                evidence_id=row['evidence_id'],
                condition_id=row['condition_id'],
                doc_id=row['doc_id'],
                ts_event=row['ts_event'],
                ts_observed=row['ts_observed'],
                polarity=row['polarity'],
                claim=row['claim'],
                reliability=row['reliability'],
                freshness_hours=row['freshness_hours'],
                extracted_values=extracted_values,
                embedding=embedding,
                created_at=row['created_at'],
            ))

        log.info(
            "evidence_search_completed",
            condition_id=condition_id,
            query_length=len(query),
            results=len(items)
        )

        return items

    async def embed_and_store(self, doc_id: str, text: str) -> None:
        """
        Generate embedding for document and store it

        Args:
            doc_id: Document ID
            text: Document text to embed
        """
        embedding = await self._get_embedding(text)
        embedding_str = f"[{','.join(map(str, embedding))}]"

        query = """
            UPDATE source_documents
            SET embedding = $1::vector
            WHERE doc_id = $2
        """

        await self.db.execute(query, embedding_str, doc_id)
        log.info("document_embedded", doc_id=doc_id)

    async def embed_evidence(self, evidence_id: str, text: str) -> None:
        """
        Generate embedding for evidence item and store it

        Args:
            evidence_id: Evidence ID
            text: Evidence claim text to embed
        """
        embedding = await self._get_embedding(text)
        embedding_str = f"[{','.join(map(str, embedding))}]"

        query = """
            UPDATE evidence_items
            SET embedding = $1::vector
            WHERE evidence_id = $2
        """

        await self.db.execute(query, embedding_str, evidence_id)
        log.info("evidence_embedded", evidence_id=evidence_id)
