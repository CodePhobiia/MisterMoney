"""
V3 Evidence Collector
Collects evidence for markets from multiple sources
"""

import hashlib
from datetime import UTC, datetime
from urllib.parse import urlparse

import aiohttp
import structlog
from bs4 import BeautifulSoup

from v3.evidence.entities import EvidenceItem
from v3.evidence.graph import EvidenceGraph
from v3.evidence.normalizer import EvidenceNormalizer
from v3.intake.schemas import MarketMeta
from v3.intake.source_checkers.base import SourceCheckResult

log = structlog.get_logger()


class EvidenceCollector:
    """
    Collects evidence for markets from multiple sources:
    1. Polymarket resolution source URL (fetch and parse)
    2. Web search (DuckDuckGo HTML search, no API key needed)
    3. Source checkers (structured data APIs)

    Stores results in the evidence DB via EvidenceGraph.
    """

    def __init__(
        self,
        evidence_graph: EvidenceGraph,
        normalizer: EvidenceNormalizer,
        timeout: float = 10.0
    ):
        """
        Initialize evidence collector

        Args:
            evidence_graph: EvidenceGraph instance for DB operations
            normalizer: EvidenceNormalizer for HTML→SourceDocument
            timeout: Timeout for HTTP requests (default 10s)
        """
        self.evidence_graph = evidence_graph
        self.normalizer = normalizer
        self.timeout = timeout

        # Rate limiting state
        self.search_count = 0
        self.search_reset_time = datetime.now(UTC)

        log.info("evidence_collector_initialized")

    async def collect_for_market(self, market: MarketMeta) -> list[EvidenceItem]:
        """
        Collect evidence for a single market:

        1. Parse resolution_source URLs from market metadata
        2. For each URL: fetch content, normalize to SourceDocument + EvidenceItem
        3. Run web search for the market question (2-3 results)
        4. Run relevant source checker if applicable
        5. Store all evidence in DB
        6. Return list of EvidenceItems

        All fetches have 10s timeout and graceful error handling.
        Skip markets we've already collected evidence for in the last hour.

        Args:
            market: MarketMeta with question, description, resolution_source

        Returns:
            List of collected EvidenceItems
        """
        log.info("collecting_evidence_start",
                condition_id=market.condition_id,
                question=market.question[:80])

        # Check if we have fresh evidence already
        if await self._has_fresh_evidence(market.condition_id):
            log.debug("evidence_fresh_skip",
                     condition_id=market.condition_id)
            return []

        evidence_items = []

        # 1. Fetch resolution source URLs
        resolution_urls = self._extract_urls(market.resolution_source)
        for url in resolution_urls[:2]:  # Max 2 URLs from resolution source
            try:
                item = await self._fetch_and_extract_url(
                    url=url,
                    condition_id=market.condition_id
                )
                if item:
                    evidence_items.append(item)
            except Exception as e:
                log.warning("resolution_url_fetch_failed",
                          url=url,
                          error=str(e))

        # 2. Run web search
        if self._can_search():
            try:
                search_results = await self._web_search(
                    query=market.question,
                    num_results=3
                )

                for result in search_results[:3]:  # Max 3 search results
                    try:
                        item = await self._extract_evidence_from_search_result(
                            result=result,
                            condition_id=market.condition_id
                        )
                        if item:
                            evidence_items.append(item)
                    except Exception as e:
                        log.warning("search_result_extract_failed",
                                  url=result.get('url'),
                                  error=str(e))
            except Exception as e:
                log.warning("web_search_failed",
                          condition_id=market.condition_id,
                          error=str(e))

        # 3. Store evidence in DB
        for item in evidence_items:
            try:
                # First upsert the source document if it has one
                if item.doc_id:
                    # Fetch the document (already stored during extraction)
                    pass

                # Then add the evidence item
                await self.evidence_graph.add_evidence(item)
            except Exception as e:
                log.warning("evidence_store_failed",
                          evidence_id=item.evidence_id,
                          error=str(e))

        log.info("collecting_evidence_complete",
                condition_id=market.condition_id,
                count=len(evidence_items))

        return evidence_items

    async def _has_fresh_evidence(
        self,
        condition_id: str,
        max_age_seconds: int = 3600
    ) -> bool:
        """
        Check if we have evidence collected within max_age_seconds

        Args:
            condition_id: Condition to check
            max_age_seconds: Maximum age of evidence in seconds (default 1 hour)

        Returns:
            True if fresh evidence exists, False otherwise
        """
        try:
            evidence = await self.evidence_graph.get_evidence_bundle(
                condition_id=condition_id,
                max_items=1
            )

            if not evidence:
                return False

            # Check age of most recent evidence
            most_recent = evidence[0]
            age = datetime.now(UTC) - most_recent.ts_observed

            is_fresh = age.total_seconds() < max_age_seconds

            log.debug("evidence_freshness_check",
                     condition_id=condition_id,
                     age_seconds=age.total_seconds(),
                     is_fresh=is_fresh)

            return is_fresh
        except Exception as e:
            log.warning("freshness_check_failed",
                       condition_id=condition_id,
                       error=str(e))
            return False

    def _extract_urls(self, text: str) -> list[str]:
        """
        Extract URLs from text

        Args:
            text: Text potentially containing URLs

        Returns:
            List of extracted URLs
        """
        import re

        # Simple URL regex
        url_pattern = r'https?://[^\s<>"{}|\\^`\[\]]+'
        urls = re.findall(url_pattern, text)

        # Filter out obviously bad URLs
        valid_urls = []
        for url in urls:
            # Clean trailing punctuation
            url = url.rstrip('.,;:!?)')

            # Basic validation
            if len(url) > 10 and '.' in url:
                valid_urls.append(url)

        return valid_urls

    async def _fetch_url(self, url: str) -> str | None:
        """
        Fetch URL content with timeout and error handling

        Args:
            url: URL to fetch

        Returns:
            HTML content or None on failure
        """
        try:
            timeout = aiohttp.ClientTimeout(total=self.timeout)
            headers = {
                'User-Agent': 'Mozilla/5.0 (compatible; MisterMoney/3.0; +https://mistermoney.io)'
            }

            async with aiohttp.ClientSession(timeout=timeout) as session:
                async with session.get(url, headers=headers, allow_redirects=True) as response:
                    if response.status in (200, 202):
                        content = await response.text()
                        log.debug("url_fetched",
                                url=url,
                                status=response.status,
                                size=len(content))
                        return content
                    else:
                        log.warning("url_fetch_failed",
                                  url=url,
                                  status=response.status)
                        return None
        except TimeoutError:
            log.warning("url_fetch_timeout", url=url)
            return None
        except Exception as e:
            log.warning("url_fetch_error", url=url, error=str(e))
            return None

    async def _fetch_and_extract_url(
        self,
        url: str,
        condition_id: str
    ) -> EvidenceItem | None:
        """
        Fetch a URL and extract evidence from it

        Args:
            url: URL to fetch
            condition_id: Market condition ID

        Returns:
            EvidenceItem or None on failure
        """
        html = await self._fetch_url(url)
        if not html:
            return None

        # Determine publisher from URL
        publisher = self._extract_publisher(url)

        # Normalize to SourceDocument
        try:
            doc = self.normalizer.normalize_article(
                raw_html=html,
                url=url,
                publisher=publisher
            )

            # Store document
            await self.evidence_graph.upsert_document(doc)

            # Extract text for claim creation
            soup = BeautifulSoup(html, 'html.parser')
            for script in soup(['script', 'style', 'nav', 'header', 'footer']):
                script.decompose()
            text = soup.get_text(separator=' ', strip=True)

            # Create evidence item with first 200 chars as claim
            claim = text[:200].strip()
            if len(text) > 200:
                claim += "..."

            # Generate evidence ID
            evidence_id = hashlib.sha256(
                f"{url}:{condition_id}".encode()
            ).hexdigest()[:16]

            # Estimate reliability
            reliability = self._estimate_publisher_reliability(url)

            item = EvidenceItem(
                evidence_id=f"url_{evidence_id}",
                condition_id=condition_id,
                doc_id=doc.doc_id,
                ts_event=None,
                ts_observed=datetime.now(UTC),
                polarity='NEUTRAL',  # Let LLM determine polarity later
                claim=claim,
                reliability=reliability,
                freshness_hours=None,
                extracted_values={'source_url': url, 'publisher': publisher},
            )

            log.info("evidence_extracted_from_url",
                    url=url,
                    evidence_id=item.evidence_id,
                    reliability=reliability)

            return item

        except Exception as e:
            log.warning("url_extraction_failed",
                       url=url,
                       error=str(e))
            return None

    def _extract_publisher(self, url: str) -> str:
        """
        Extract publisher name from URL

        Args:
            url: URL to extract publisher from

        Returns:
            Publisher name (domain)
        """
        try:
            parsed = urlparse(url)
            domain = parsed.netloc

            # Remove 'www.' prefix
            if domain.startswith('www.'):
                domain = domain[4:]

            # Remove TLD for cleaner name
            parts = domain.split('.')
            if len(parts) > 1:
                return parts[0]

            return domain
        except Exception:
            return 'unknown'

    def _estimate_publisher_reliability(self, url: str) -> float:
        """
        Estimate reliability based on domain

        Args:
            url: URL to estimate reliability for

        Returns:
            Reliability score 0.0-1.0
        """
        domain = urlparse(url).netloc.lower()

        # Tier 1: Highly reliable (0.85-0.95)
        tier1 = [
            'reuters.com', 'apnews.com', 'bbc.com', 'bbc.co.uk',
            'bloomberg.com', 'wsj.com', 'ft.com',
        ]

        # Tier 2: Reliable (0.7-0.8)
        tier2 = [
            'nytimes.com', 'washingtonpost.com', 'theguardian.com',
            'cnbc.com', 'cnn.com', 'foxnews.com', 'nbcnews.com',
            'forbes.com', 'businessinsider.com',
        ]

        # Tier 3: Somewhat reliable (0.5-0.6)
        tier3 = [
            'techcrunch.com', 'theverge.com', 'engadget.com',
            'polygon.com', 'ign.com', 'variety.com',
        ]

        # Tier 4: Low reliability (0.2-0.3)
        tier4 = [
            'twitter.com', 'reddit.com', 'facebook.com',
            'instagram.com', 'tiktok.com',
        ]

        for trusted in tier1:
            if trusted in domain:
                return 0.9

        for reliable in tier2:
            if reliable in domain:
                return 0.75

        for decent in tier3:
            if decent in domain:
                return 0.55

        for low in tier4:
            if low in domain:
                return 0.25

        # Default for unknown sources
        return 0.5

    def _can_search(self) -> bool:
        """
        Check if we can perform a web search (rate limiting)

        Max 20 searches per cycle (5 minutes)

        Returns:
            True if search is allowed, False otherwise
        """
        now = datetime.now(UTC)

        # Reset counter every 5 minutes
        if (now - self.search_reset_time).total_seconds() > 300:
            self.search_count = 0
            self.search_reset_time = now

        if self.search_count >= 20:
            log.debug("search_rate_limit_hit", count=self.search_count)
            return False

        return True

    async def _web_search(
        self,
        query: str,
        num_results: int = 3
    ) -> list[dict]:
        """
        Search the web for recent news about the market question

        Uses duckduckgo-search library (avoids CAPTCHA on HTML endpoint)

        Args:
            query: Search query
            num_results: Number of results to return

        Returns:
            List of {title, url, snippet} dicts
        """
        self.search_count += 1

        log.info("web_search_start", query=query[:80])

        try:
            import asyncio

            from duckduckgo_search import DDGS

            # Run sync DDG search in thread to avoid blocking
            def _search():
                with DDGS() as d:
                    return list(d.text(query, max_results=num_results))

            raw_results = await asyncio.get_event_loop().run_in_executor(
                None, _search
            )

            results = []
            for r in raw_results:
                if r.get('href') and r.get('title'):
                    results.append({
                        'title': r['title'],
                        'url': r['href'],
                        'snippet': r.get('body', r['title'])
                    })

            log.info("web_search_complete",
                    query=query[:80],
                    count=len(results))

            return results

        except Exception as e:
            log.error("web_search_error", query=query, error=str(e))
            return []

    async def _extract_evidence_from_search_result(
        self,
        result: dict,
        condition_id: str
    ) -> EvidenceItem | None:
        """
        Fetch a search result URL, extract text content, create EvidenceItem

        Args:
            result: Search result dict with title, url, snippet
            condition_id: Market condition ID

        Returns:
            EvidenceItem or None on failure
        """
        url = result.get('url')
        if not url:
            return None

        # Try to fetch and extract the URL
        item = await self._fetch_and_extract_url(
            url=url,
            condition_id=condition_id
        )

        # If fetch failed, create evidence from snippet
        if not item:
            snippet = result.get('snippet', '')
            title = result.get('title', '')

            if snippet or title:
                claim = f"{title}. {snippet}".strip()[:200]

                evidence_id = hashlib.sha256(
                    f"{url}:{condition_id}:snippet".encode()
                ).hexdigest()[:16]

                item = EvidenceItem(
                    evidence_id=f"snippet_{evidence_id}",
                    condition_id=condition_id,
                    doc_id=None,  # No document, just snippet
                    ts_event=None,
                    ts_observed=datetime.now(UTC),
                    polarity='NEUTRAL',
                    claim=claim,
                    reliability=0.4,  # Lower reliability for snippets
                    freshness_hours=None,
                    extracted_values={
                        'source_url': url,
                        'title': title,
                        'snippet': snippet,
                    },
                )

                log.debug("evidence_from_snippet",
                         url=url,
                         evidence_id=item.evidence_id)

        return item

    async def _run_source_checker(
        self,
        market: MarketMeta
    ) -> SourceCheckResult | None:
        """
        Run appropriate source checker based on market classification

        This is a placeholder for future integration with source checkers.
        For now, we'll skip this since it requires market classification
        and rule parsing which is already done in the orchestrator.

        Args:
            market: Market metadata

        Returns:
            SourceCheckResult or None
        """
        # TODO: Integrate source checkers
        # Would need to:
        # 1. Classify market (crypto, sports, economic, etc.)
        # 2. Get or create rule graph
        # 3. Call appropriate checker
        # For now, return None
        return None
