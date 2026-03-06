"""Geoblock check — verify we can access Polymarket from this IP."""

from __future__ import annotations

import aiohttp
import structlog

logger = structlog.get_logger(__name__)


class GeoblockError(Exception):
    """Raised when the bot is running from a geoblocked region."""


async def check_geoblock(
    geoblock_url: str = "https://polymarket.com",
    timeout_s: int = 10,
) -> bool:
    """Check if the current IP is geoblocked by Polymarket.

    Polymarket returns a redirect or specific response for blocked regions.
    The check lives on polymarket.com, NOT the API hosts.

    Returns:
        True if NOT blocked (access OK).

    Raises:
        GeoblockError: If the IP appears to be blocked.
    """
    try:
        async with aiohttp.ClientSession(
            timeout=aiohttp.ClientTimeout(total=timeout_s)
        ) as session:
            async with session.get(
                geoblock_url,
                allow_redirects=False,
                headers={
                    "User-Agent": "PMM-1/0.1",
                    "Accept": "text/html",
                },
            ) as resp:
                # Geoblocked users get redirected to a blocked page
                # or receive specific status codes
                if resp.status in (451, 403):
                    logger.error(
                        "geoblock_detected",
                        status=resp.status,
                        url=geoblock_url,
                    )
                    raise GeoblockError(
                        f"Geoblocked: HTTP {resp.status} from {geoblock_url}. "
                        "Orders from blocked regions will be rejected."
                    )

                # Check for redirect to geo-block page
                if resp.status in (301, 302, 307, 308):
                    location = resp.headers.get("Location", "")
                    if "blocked" in location.lower() or "geo" in location.lower():
                        logger.error(
                            "geoblock_redirect",
                            location=location,
                            url=geoblock_url,
                        )
                        raise GeoblockError(
                            f"Geoblocked: redirected to {location}"
                        )

                # Check response body for block indicators
                if resp.status == 200:
                    body = await resp.text()
                    block_indicators = [
                        "not available in your region",
                        "geographic restrictions",
                        "geo-restricted",
                        "access denied",
                    ]
                    body_lower = body.lower()
                    for indicator in block_indicators:
                        if indicator in body_lower:
                            logger.error(
                                "geoblock_body_indicator",
                                indicator=indicator,
                                url=geoblock_url,
                            )
                            raise GeoblockError(
                                f"Geoblocked: page contains '{indicator}'"
                            )

                logger.info("geoblock_check_passed", status=resp.status)
                return True

    except GeoblockError:
        raise
    except aiohttp.ClientError as e:
        logger.warning("geoblock_check_error", error=str(e))
        # Network error — not necessarily blocked, but we should be cautious
        # Allow startup but log warning
        return True
    except Exception as e:
        logger.warning("geoblock_check_unexpected", error=str(e))
        return True
