"""
V3 Object Storage Adapter
Local filesystem storage with S3-compatible interface
"""

from pathlib import Path

import aiofiles
import aiofiles.os
import structlog

log = structlog.get_logger()


class ObjectStore:
    """Local filesystem object storage with async interface"""

    def __init__(self, base_path: str = "data/v3/objects"):
        """
        Initialize object store

        Args:
            base_path: Base directory for object storage
        """
        self.base_path = Path(base_path)
        self.base_path.mkdir(parents=True, exist_ok=True)
        log.info("object_store_initialized", path=str(self.base_path))

    def _get_path(self, key: str) -> Path:
        """
        Get filesystem path for a key

        Uses simple key-to-path mapping with subdirectories based on key prefix
        to avoid too many files in one directory.

        Args:
            key: Object key (e.g., "docs/article_123.txt")

        Returns:
            Full filesystem path
        """
        # Sanitize key (replace any path traversal attempts)
        safe_key = key.replace("..", "").lstrip("/")

        # Split into prefix and filename for directory structure
        parts = safe_key.split("/")

        if len(parts) > 1:
            # Use directory structure from key
            path = self.base_path / Path(*parts)
        else:
            # Single-level key, use first 2 chars as prefix dir
            prefix = safe_key[:2] if len(safe_key) >= 2 else "00"
            path = self.base_path / prefix / safe_key

        return path

    async def put(
        self,
        key: str,
        content: bytes,
        content_type: str = "text/plain"
    ) -> str:
        """
        Store an object

        Args:
            key: Object key
            content: Binary content to store
            content_type: MIME type (stored in metadata file)

        Returns:
            Key of stored object
        """
        path = self._get_path(key)
        path.parent.mkdir(parents=True, exist_ok=True)

        # Write content
        async with aiofiles.open(path, 'wb') as f:
            await f.write(content)

        # Write metadata
        metadata_path = path.parent / f"{path.name}.meta"
        async with aiofiles.open(metadata_path, 'w') as f:
            await f.write(f"content_type: {content_type}\n")
            await f.write(f"size: {len(content)}\n")

        log.info(
            "object_stored",
            key=key,
            size=len(content),
            content_type=content_type
        )

        return key

    async def get(self, key: str) -> bytes:
        """
        Retrieve an object

        Args:
            key: Object key

        Returns:
            Binary content

        Raises:
            FileNotFoundError: If object doesn't exist
        """
        path = self._get_path(key)

        if not path.exists():
            log.warning("object_not_found", key=key)
            raise FileNotFoundError(f"Object not found: {key}")

        async with aiofiles.open(path, 'rb') as f:
            content = await f.read()

        log.info("object_retrieved", key=key, size=len(content))
        return content

    async def exists(self, key: str) -> bool:
        """
        Check if an object exists

        Args:
            key: Object key

        Returns:
            True if object exists, False otherwise
        """
        path = self._get_path(key)
        exists = path.exists()

        log.debug("object_existence_checked", key=key, exists=exists)
        return exists

    async def delete(self, key: str) -> None:
        """
        Delete an object

        Args:
            key: Object key
        """
        path = self._get_path(key)
        metadata_path = path.parent / f"{path.name}.meta"

        if path.exists():
            await aiofiles.os.remove(path)
            log.info("object_deleted", key=key)
        else:
            log.warning("object_delete_not_found", key=key)

        # Clean up metadata
        if metadata_path.exists():
            await aiofiles.os.remove(metadata_path)
