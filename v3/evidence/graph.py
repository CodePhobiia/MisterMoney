"""
V3 Evidence Graph CRUD Operations
Create, read, update, delete operations for evidence layer
"""

import json

import structlog

from .db import Database
from .entities import (
    EvidenceItem,
    FairValueSignal,
    RuleGraph,
    SourceDocument,
)

log = structlog.get_logger()


class EvidenceGraph:
    """CRUD operations for evidence graph"""

    def __init__(self, db: Database):
        """
        Initialize evidence graph with database connection

        Args:
            db: Database instance
        """
        self.db = db

    async def upsert_document(self, doc: SourceDocument) -> str:
        """
        Insert or update a source document

        Args:
            doc: SourceDocument to upsert

        Returns:
            doc_id of the upserted document
        """
        query = """
            INSERT INTO source_documents (
                doc_id, url, source_type, publisher, fetched_at,
                content_hash, title, text_path, metadata, embedding, created_at
            ) VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11)
            ON CONFLICT (doc_id) DO UPDATE SET
                url = EXCLUDED.url,
                source_type = EXCLUDED.source_type,
                publisher = EXCLUDED.publisher,
                fetched_at = EXCLUDED.fetched_at,
                content_hash = EXCLUDED.content_hash,
                title = EXCLUDED.title,
                text_path = EXCLUDED.text_path,
                metadata = EXCLUDED.metadata,
                embedding = EXCLUDED.embedding
            RETURNING doc_id
        """

        embedding_str = f"[{','.join(map(str, doc.embedding))}]" if doc.embedding else None

        result = await self.db.fetchrow(
            query,
            doc.doc_id,
            doc.url,
            doc.source_type,
            doc.publisher,
            doc.fetched_at,
            doc.content_hash,
            doc.title,
            doc.text_path,
            json.dumps(doc.metadata),
            embedding_str,
            doc.created_at,
        )

        log.info("document_upserted", doc_id=doc.doc_id)
        return result['doc_id']

    async def add_evidence(self, item: EvidenceItem) -> str:
        """
        Add an evidence item

        Args:
            item: EvidenceItem to add

        Returns:
            evidence_id of the added item
        """
        query = """
            INSERT INTO evidence_items (
                evidence_id, condition_id, doc_id, ts_event, ts_observed,
                polarity, claim, reliability, freshness_hours,
                extracted_values, embedding, created_at
            ) VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, $12)
            ON CONFLICT (evidence_id) DO UPDATE SET
                condition_id = EXCLUDED.condition_id,
                doc_id = EXCLUDED.doc_id,
                ts_event = EXCLUDED.ts_event,
                ts_observed = EXCLUDED.ts_observed,
                polarity = EXCLUDED.polarity,
                claim = EXCLUDED.claim,
                reliability = EXCLUDED.reliability,
                freshness_hours = EXCLUDED.freshness_hours,
                extracted_values = EXCLUDED.extracted_values,
                embedding = EXCLUDED.embedding
            RETURNING evidence_id
        """

        embedding_str = f"[{','.join(map(str, item.embedding))}]" if item.embedding else None

        result = await self.db.fetchrow(
            query,
            item.evidence_id,
            item.condition_id,
            item.doc_id,
            item.ts_event,
            item.ts_observed,
            item.polarity,
            item.claim,
            item.reliability,
            item.freshness_hours,
            json.dumps(item.extracted_values),
            embedding_str,
            item.created_at,
        )

        log.info("evidence_added", evidence_id=item.evidence_id, condition_id=item.condition_id)
        return result['evidence_id']

    async def upsert_rule_graph(self, rule: RuleGraph) -> None:
        """
        Insert or update a rule graph

        Args:
            rule: RuleGraph to upsert
        """
        query = """
            INSERT INTO rule_graphs (
                condition_id, source_name, operator, threshold_num,
                threshold_text, window_start, window_end, edge_cases,
                clarification_ids, updated_at, created_at
            ) VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11)
            ON CONFLICT (condition_id) DO UPDATE SET
                source_name = EXCLUDED.source_name,
                operator = EXCLUDED.operator,
                threshold_num = EXCLUDED.threshold_num,
                threshold_text = EXCLUDED.threshold_text,
                window_start = EXCLUDED.window_start,
                window_end = EXCLUDED.window_end,
                edge_cases = EXCLUDED.edge_cases,
                clarification_ids = EXCLUDED.clarification_ids,
                updated_at = EXCLUDED.updated_at
        """

        await self.db.execute(
            query,
            rule.condition_id,
            rule.source_name,
            rule.operator,
            rule.threshold_num,
            rule.threshold_text,
            rule.window_start,
            rule.window_end,
            json.dumps(rule.edge_cases),
            json.dumps(rule.clarification_ids),
            rule.updated_at,
            rule.created_at,
        )

        log.info("rule_graph_upserted", condition_id=rule.condition_id)

    async def get_evidence_bundle(
        self,
        condition_id: str,
        max_items: int = 20
    ) -> list[EvidenceItem]:
        """
        Get recent evidence for a condition

        Args:
            condition_id: Condition to get evidence for
            max_items: Maximum number of items to return

        Returns:
            List of EvidenceItems, most recent first
        """
        query = """
            SELECT * FROM evidence_items
            WHERE condition_id = $1
            ORDER BY ts_observed DESC
            LIMIT $2
        """

        rows = await self.db.fetch(query, condition_id, max_items)

        items = []
        for row in rows:
            # Convert embedding from string to list if present
            embedding = None
            if row.get('embedding'):
                # asyncpg returns vectors as strings like '[1,2,3]'
                embedding_str = str(row['embedding'])
                if embedding_str.startswith('[') and embedding_str.endswith(']'):
                    embedding = [float(x) for x in embedding_str[1:-1].split(',')]

            # Parse extracted_values if it's a string
            # (asyncpg returns JSONB as dict already)
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

        log.info("evidence_bundle_fetched", condition_id=condition_id, count=len(items))
        return items

    async def get_rule_graph(self, condition_id: str) -> RuleGraph | None:
        """
        Get rule graph for a condition

        Args:
            condition_id: Condition to get rule graph for

        Returns:
            RuleGraph or None if not found
        """
        query = "SELECT * FROM rule_graphs WHERE condition_id = $1"
        row = await self.db.fetchrow(query, condition_id)

        if not row:
            log.info("rule_graph_not_found", condition_id=condition_id)
            return None

        # Parse JSONB fields
        edge_cases = row['edge_cases']
        if isinstance(edge_cases, str):
            edge_cases = json.loads(edge_cases)

        clarification_ids = row['clarification_ids']
        if isinstance(clarification_ids, str):
            clarification_ids = json.loads(clarification_ids)

        rule = RuleGraph(
            condition_id=row['condition_id'],
            source_name=row['source_name'],
            operator=row['operator'],
            threshold_num=row['threshold_num'],
            threshold_text=row['threshold_text'],
            window_start=row['window_start'],
            window_end=row['window_end'],
            edge_cases=edge_cases,
            clarification_ids=clarification_ids,
            updated_at=row['updated_at'],
            created_at=row['created_at'],
        )

        log.info("rule_graph_fetched", condition_id=condition_id)
        return rule

    async def get_document(self, doc_id: str) -> SourceDocument | None:
        """
        Get a source document by ID

        Args:
            doc_id: Document ID to fetch

        Returns:
            SourceDocument or None if not found
        """
        query = "SELECT * FROM source_documents WHERE doc_id = $1"
        row = await self.db.fetchrow(query, doc_id)

        if not row:
            log.info("document_not_found", doc_id=doc_id)
            return None

        # Convert embedding from string to list if present
        embedding = None
        if row.get('embedding'):
            embedding_str = str(row['embedding'])
            if embedding_str.startswith('[') and embedding_str.endswith(']'):
                embedding = [float(x) for x in embedding_str[1:-1].split(',')]

        # Parse metadata JSONB
        metadata = row['metadata']
        if isinstance(metadata, str):
            metadata = json.loads(metadata)

        doc = SourceDocument(
            doc_id=row['doc_id'],
            url=row['url'],
            source_type=row['source_type'],
            publisher=row['publisher'],
            fetched_at=row['fetched_at'],
            content_hash=row['content_hash'],
            title=row['title'],
            text_path=row['text_path'],
            metadata=metadata,
            embedding=embedding,
            created_at=row['created_at'],
        )

        log.info("document_fetched", doc_id=doc_id)
        return doc

    async def deduplicate(self, condition_id: str) -> int:
        """
        Remove duplicate evidence items by content_hash
        Keeps the most recent version of each unique claim

        Args:
            condition_id: Condition to deduplicate evidence for

        Returns:
            Number of duplicates removed
        """
        # This query finds duplicates by matching doc content_hash
        # and keeps only the most recent evidence_item per doc
        query = """
            WITH duplicates AS (
                SELECT
                    e.evidence_id,
                    ROW_NUMBER() OVER (
                        PARTITION BY d.content_hash, e.condition_id
                        ORDER BY e.ts_observed DESC
                    ) as rn
                FROM evidence_items e
                JOIN source_documents d ON e.doc_id = d.doc_id
                WHERE e.condition_id = $1
            )
            DELETE FROM evidence_items
            WHERE evidence_id IN (
                SELECT evidence_id FROM duplicates WHERE rn > 1
            )
        """

        result = await self.db.execute(query, condition_id)

        # Parse result like "DELETE 3"
        count = int(result.split()[-1]) if result.split()[-1].isdigit() else 0

        log.info("evidence_deduplicated", condition_id=condition_id, removed=count)
        return count

    async def save_signal(self, signal: FairValueSignal) -> None:
        """
        Save a fair value signal

        Args:
            signal: FairValueSignal to save
        """
        query = """
            INSERT INTO fair_value_signals (
                condition_id, generated_at, p_calibrated, p_low, p_high,
                uncertainty, skew_cents, hurdle_cents, hurdle_met, route,
                evidence_ids, counterevidence_ids, models_used, expires_at, created_at
            ) VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, $12, $13, $14, $15)
            ON CONFLICT (condition_id, generated_at) DO UPDATE SET
                p_calibrated = EXCLUDED.p_calibrated,
                p_low = EXCLUDED.p_low,
                p_high = EXCLUDED.p_high,
                uncertainty = EXCLUDED.uncertainty,
                skew_cents = EXCLUDED.skew_cents,
                hurdle_cents = EXCLUDED.hurdle_cents,
                hurdle_met = EXCLUDED.hurdle_met,
                route = EXCLUDED.route,
                evidence_ids = EXCLUDED.evidence_ids,
                counterevidence_ids = EXCLUDED.counterevidence_ids,
                models_used = EXCLUDED.models_used,
                expires_at = EXCLUDED.expires_at
        """

        await self.db.execute(
            query,
            signal.condition_id,
            signal.generated_at,
            signal.p_calibrated,
            signal.p_low,
            signal.p_high,
            signal.uncertainty,
            signal.skew_cents,
            signal.hurdle_cents,
            signal.hurdle_met,
            signal.route,
            json.dumps(signal.evidence_ids),
            json.dumps(signal.counterevidence_ids),
            json.dumps(signal.models_used),
            signal.expires_at,
            signal.created_at,
        )

        log.info("signal_saved", condition_id=signal.condition_id)

    async def get_latest_signal(self, condition_id: str) -> FairValueSignal | None:
        """
        Get the most recent fair value signal for a condition

        Args:
            condition_id: Condition to get signal for

        Returns:
            FairValueSignal or None if not found
        """
        query = """
            SELECT * FROM fair_value_signals
            WHERE condition_id = $1
            ORDER BY generated_at DESC
            LIMIT 1
        """

        row = await self.db.fetchrow(query, condition_id)

        if not row:
            log.info("signal_not_found", condition_id=condition_id)
            return None

        # Parse JSONB fields
        evidence_ids = row['evidence_ids']
        if isinstance(evidence_ids, str):
            evidence_ids = json.loads(evidence_ids)

        counterevidence_ids = row['counterevidence_ids']
        if isinstance(counterevidence_ids, str):
            counterevidence_ids = json.loads(counterevidence_ids)

        models_used = row['models_used']
        if isinstance(models_used, str):
            models_used = json.loads(models_used)

        signal = FairValueSignal(
            condition_id=row['condition_id'],
            generated_at=row['generated_at'],
            p_calibrated=row['p_calibrated'],
            p_low=row['p_low'],
            p_high=row['p_high'],
            uncertainty=row['uncertainty'],
            skew_cents=row['skew_cents'],
            hurdle_cents=row['hurdle_cents'],
            hurdle_met=row['hurdle_met'],
            route=row['route'],
            evidence_ids=evidence_ids,
            counterevidence_ids=counterevidence_ids,
            models_used=models_used,
            expires_at=row['expires_at'],
            created_at=row['created_at'],
        )

        log.info("signal_fetched", condition_id=condition_id)
        return signal
