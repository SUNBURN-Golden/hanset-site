"""Public web collection and desktop Instagram ingestion.

This package does not browse Instagram, read a browser session, or publish
site facts. Web fetches use the existing guarded HTTP fetcher. Instagram
input is a transient desktop batch.
"""

__all__ = ['CollectError']


class CollectError(RuntimeError):
    """Fixed operational code. The message is the code and nothing else."""

    def __init__(self, reason):
        self.reason = reason
        super().__init__(reason)
