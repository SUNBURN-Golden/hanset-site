"""Permission gate only. This module performs no I/O or browser operations."""
import os
from ..util.timeutil import kst_now

class ConnectorPaused(RuntimeError):
    pass

class InstagramConnector:
    def __init__(self, session_state_path=None, *, clock=None):
        self.session_state_path = session_state_path
        self.clock = clock

    def read_profile(self, handle):
        if not (os.environ.get('HANSET_IG_ENABLED') == '1' and
                kst_now(self.clock).hour == 13 and self.session_state_path):
            raise ConnectorPaused('instagram connector paused')
        # The gate can open, but collection is deliberately unimplemented in v1.
        return None
