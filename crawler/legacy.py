"""Read-only conservative mapping: summary numbers are not processing evidence."""
import json
from pathlib import Path
from .schema import STAGES


def report(path):
    data = json.loads(Path(path).read_text())
    stages = data.get('stages', {})
    candidates = stages.get('1_discovered', []) + stages.get('2_needs_official_check', [])
    published = stages.get('4_published_or_expired', {})
    return {'auto_publish_to_site': False,
            'stage_counts': {s: len(candidates) if i == 0 else 0 for i, s in enumerate(STAGES)},
            'missing_url': sum(not any(x.get(k) for k in ('url', 'source_url', 'official_url')) for x in candidates),
            'legacy_published_venues': len(published.get('published_open_venues', [])),
            'legacy_summary_only': data.get('stage_counts', {}),
            'provenance': 'individual_inventory_only; no processing history inferred'}
