"""Production limits cannot be raised by profile or environment."""
import json
import math
import os
from dataclasses import dataclass, field
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]

def load_profile(path: str | Path | None = None) -> dict:
    with open(path or Path(__file__).parent / 'profiles/hanset.json', encoding='utf-8') as f:
        return json.load(f)

@dataclass
class Settings:
    data_dir: Path = field(default_factory=lambda: Path(os.environ.get('HANSET_ACTIVE_DIR', '/workspace/hanset-data/active')))
    min_interval: float = 0.0
    global_gap: float = 0.0
    candidate_cap: int = 500
    body_cap: int = 200
    aux_cap: int = 800
    testing: bool = False
    allow_private_hosts: bool = False
    timeout: float = 15.0
    profile_path: Path | None = None
    def __post_init__(self):
        self.data_dir = Path(self.data_dir).resolve()
        if self.data_dir == REPO_ROOT or REPO_ROOT in self.data_dir.parents:
            raise ValueError('Data directory must be outside checkout')
        if not math.isfinite(self.min_interval) or self.min_interval < 0:
            raise ValueError('News spacing must be finite and nonnegative')
        if not math.isfinite(self.global_gap) or self.global_gap < 0:
            raise ValueError('Global gap must be finite and nonnegative')
        for name, cap in [('candidate_cap',500),('body_cap',200),('aux_cap',800)]:
            if not isinstance(getattr(self,name),int) or not 0 <= getattr(self,name) <= cap:
                raise ValueError('Daily cap cannot be raised')
        if self.allow_private_hosts and not self.testing:
            raise ValueError('Private fixtures require testing=True')
    @property
    def profile(self):
        return load_profile(self.profile_path)
