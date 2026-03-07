"""
V3 Evidence Normalizer
Extract structured evidence from raw sources
"""

import re
import hashlib
from datetime import datetime, timezone
from typing import List
from bs4 import BeautifulSoup
import structlog

from .entities import SourceDocument, EvidenceItem

log = structlog.get_logger()


class EvidenceNormalizer:
    """Extract and normalize evidence from various sources"""
    
    def normalize_article(
        self,
        raw_html: str,
        url: str,
        publisher: str
    ) -> SourceDocument:
        """
        Normalize HTML article into SourceDocument
        
        Args:
            raw_html: Raw HTML content
            url: Article URL
            publisher: Publisher name
            
        Returns:
            SourceDocument with extracted metadata
        """
        soup = BeautifulSoup(raw_html, 'html.parser')
        
        # Extract title
        title = None
        if soup.title:
            title = soup.title.string
        elif soup.find('h1'):
            title = soup.find('h1').get_text()
            
        # Extract text content (remove scripts, styles)
        for script in soup(['script', 'style', 'nav', 'header', 'footer']):
            script.decompose()
            
        text = soup.get_text(separator=' ', strip=True)
        
        # Generate content hash
        content_hash = hashlib.sha256(text.encode()).hexdigest()[:16]
        
        # Generate doc_id
        doc_id = f"article_{publisher}_{content_hash}"
        
        # Extract metadata
        metadata = {
            'html_length': len(raw_html),
            'text_length': len(text),
        }
        
        # Try to extract publish date from common meta tags
        publish_date = None
        for meta in soup.find_all('meta'):
            if meta.get('property') in ['article:published_time', 'datePublished']:
                publish_date = meta.get('content')
                break
            elif meta.get('name') in ['pubdate', 'publishdate', 'date']:
                publish_date = meta.get('content')
                break
                
        if publish_date:
            metadata['publish_date'] = publish_date
            
        doc = SourceDocument(
            doc_id=doc_id,
            url=url,
            source_type='article',
            publisher=publisher,
            fetched_at=datetime.now(timezone.utc),
            content_hash=content_hash,
            title=title,
            text_path=None,  # Will be set when stored
            metadata=metadata,
        )
        
        log.info(
            "article_normalized",
            doc_id=doc_id,
            publisher=publisher,
            title_length=len(title) if title else 0
        )
        
        return doc
        
    def normalize_api_response(
        self,
        data: dict,
        source_name: str
    ) -> SourceDocument:
        """
        Normalize API response into SourceDocument
        
        Args:
            data: API response data (dict)
            source_name: Name of API source (e.g., 'polymarket', 'twitter')
            
        Returns:
            SourceDocument with normalized data
        """
        import json
        
        # Serialize data
        text = json.dumps(data, sort_keys=True)
        content_hash = hashlib.sha256(text.encode()).hexdigest()[:16]
        
        doc_id = f"api_{source_name}_{content_hash}"
        
        # Extract title if available
        title = data.get('title') or data.get('name') or data.get('question')
        
        # Build metadata
        metadata = {
            'source': source_name,
            'raw_keys': list(data.keys()),
        }
        
        doc = SourceDocument(
            doc_id=doc_id,
            url=data.get('url'),
            source_type='api',
            publisher=source_name,
            fetched_at=datetime.now(timezone.utc),
            content_hash=content_hash,
            title=title,
            text_path=None,
            metadata=metadata,
        )
        
        log.info(
            "api_response_normalized",
            doc_id=doc_id,
            source=source_name,
            keys=len(data.keys())
        )
        
        return doc
        
    def extract_claims_deterministic(
        self,
        doc: SourceDocument,
        text: str
    ) -> List[EvidenceItem]:
        """
        Extract claims using deterministic rules (regex, NER, etc.)
        
        LLM-based extraction deferred to later sprints.
        
        Args:
            doc: Source document
            text: Full text content
            
        Returns:
            List of EvidenceItems extracted from text
        """
        claims = []
        
        # Pattern 1: Numbers with context
        # e.g., "revenue increased by 15%", "GDP growth of 3.2%"
        number_pattern = r'(\w+(?:\s+\w+){0,3})\s+(increased|decreased|grew|fell|rose|dropped|reached|hit|topped)\s+(?:by\s+)?([0-9.]+)(%|million|billion|thousand|dollars|cents)'
        
        for match in re.finditer(number_pattern, text, re.IGNORECASE):
            subject = match.group(1).strip()
            action = match.group(2).lower()
            value = match.group(3)
            unit = match.group(4)
            
            claim_text = f"{subject} {action} {value}{unit}"
            
            # Determine polarity based on action
            if action in ['increased', 'grew', 'rose', 'reached', 'hit', 'topped']:
                polarity = 'YES'
            elif action in ['decreased', 'fell', 'dropped']:
                polarity = 'NO'
            else:
                polarity = 'NEUTRAL'
                
            evidence_id = f"{doc.doc_id}_claim_{len(claims)}"
            
            claims.append(EvidenceItem(
                evidence_id=evidence_id,
                condition_id='UNKNOWN',  # Will be set by caller
                doc_id=doc.doc_id,
                ts_event=None,
                ts_observed=datetime.now(timezone.utc),
                polarity=polarity,
                claim=claim_text,
                reliability=0.6,  # Medium confidence for regex extraction
                freshness_hours=None,
                extracted_values={
                    'subject': subject,
                    'action': action,
                    'value': float(value),
                    'unit': unit,
                },
            ))
            
        # Pattern 2: Date-based events
        # e.g., "on January 15, 2024", "scheduled for March 2024"
        date_pattern = r'(?:on|by|before|after|during|in)\s+([A-Z][a-z]+\s+\d{1,2},?\s+\d{4}|[A-Z][a-z]+\s+\d{4}|\d{1,2}/\d{1,2}/\d{4})'
        
        for match in re.finditer(date_pattern, text):
            date_str = match.group(1)
            
            # Get surrounding context
            start = max(0, match.start() - 50)
            end = min(len(text), match.end() + 50)
            context = text[start:end].strip()
            
            evidence_id = f"{doc.doc_id}_claim_{len(claims)}"
            
            claims.append(EvidenceItem(
                evidence_id=evidence_id,
                condition_id='UNKNOWN',
                doc_id=doc.doc_id,
                ts_event=None,  # Would need date parsing
                ts_observed=datetime.now(timezone.utc),
                polarity='NEUTRAL',
                claim=context,
                reliability=0.5,  # Lower confidence for date extraction
                freshness_hours=None,
                extracted_values={
                    'date_mention': date_str,
                },
            ))
            
        # Pattern 3: Named entities with superlatives
        # e.g., "first company to", "largest ever", "highest recorded"
        superlative_pattern = r'(first|largest|biggest|smallest|highest|lowest|fastest|slowest|best|worst)\s+(\w+(?:\s+\w+){0,3})\s+(?:to|in|of|for)'
        
        for match in re.finditer(superlative_pattern, text, re.IGNORECASE):
            superlative = match.group(1).lower()
            subject = match.group(2).strip()
            
            # Get context
            start = max(0, match.start() - 30)
            end = min(len(text), match.end() + 70)
            context = text[start:end].strip()
            
            # Positive superlatives
            if superlative in ['first', 'largest', 'biggest', 'highest', 'fastest', 'best']:
                polarity = 'YES'
            elif superlative in ['smallest', 'lowest', 'slowest', 'worst']:
                polarity = 'NO'
            else:
                polarity = 'NEUTRAL'
                
            evidence_id = f"{doc.doc_id}_claim_{len(claims)}"
            
            claims.append(EvidenceItem(
                evidence_id=evidence_id,
                condition_id='UNKNOWN',
                doc_id=doc.doc_id,
                ts_event=None,
                ts_observed=datetime.now(timezone.utc),
                polarity=polarity,
                claim=context,
                reliability=0.55,
                freshness_hours=None,
                extracted_values={
                    'superlative': superlative,
                    'subject': subject,
                },
            ))
            
        log.info(
            "claims_extracted",
            doc_id=doc.doc_id,
            claim_count=len(claims),
            method='deterministic'
        )
        
        return claims
