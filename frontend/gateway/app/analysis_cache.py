"""Evidence-aware persisted analysis; reading never triggers a provider call."""
from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime, timezone
import fcntl
import hashlib
import json
import math
import os
from pathlib import Path
import tempfile
import time

_VOLATILE = {'observedAt', 'freshness', 'updatedAt', 'lastSeen', 'firstSeen', 'timestamp',
             'generatedAt', 'ageSeconds', 'ageSec', 'fetchedAt', 'lastObservedAt',
             'lastSeenAt', 'firstSeenAt', 'checked_at', 'updated_at', 'last_seen', 'first_seen'}
_COUNTS = {'events', 'eventCount', 'count', 'bytes', 'packets', 'captures', 'samples', 'flows'}


def stable_evidence(value):
    if isinstance(value, dict):
        return {key: ((int(math.log2(item)) + 1 if item >= 1 else 0) if key in _COUNTS and isinstance(item, (int, float)) and item >= 0
                      else stable_evidence(item))
                for key, item in sorted(value.items()) if key not in _VOLATILE}
    if isinstance(value, list):
        return sorted((stable_evidence(item) for item in value), key=lambda item: json.dumps(item, sort_keys=True))
    return value


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False).encode()).hexdigest()


def cache_paths(identity):
    directory = Path(os.getenv('AUTOPOIESIS_LLM_CACHE_DIR', '/data/autopoiesis-production/llm-cache')) / 'evidence-analysis-v1'
    directory.mkdir(parents=True, exist_ok=True)
    name = digest(identity)
    return directory / (name + '.json'), directory / (name + '.lock')


def read_entry(path):
    try:
        value = json.loads(path.read_text())
        return value if isinstance(value, dict) else None
    except (OSError, ValueError):
        return None


@contextmanager
def analysis_lock(path):
    # Process-shared lock prevents concurrent gateway workers paying twice.
    with path.open('a') as handle:
        fcntl.flock(handle, fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(handle, fcntl.LOCK_UN)


def iso(value):
    return datetime.fromtimestamp(value, timezone.utc).isoformat()


def metadata(entry, fingerprint):
    changed = bool(entry and entry.get('fingerprint') != fingerprint)
    valid = bool(entry and not changed and entry.get('expires', 0) > time.time())
    return {'status': 'hit' if valid else 'stale' if entry else 'miss',
            'analyzedAt': iso(entry['created']) if entry else None,
            'expiresAt': iso(entry['expires']) if entry else None,
            'evidenceChanged': changed}


def write_entry(path, fingerprint, result):
    now = time.time()
    entry = {'created': now, 'expires': now + max(60, float(os.getenv('AUTOPOIESIS_ANALYSIS_TTL', '3600'))),
             'fingerprint': fingerprint, 'result': result}
    descriptor, temporary = tempfile.mkstemp(dir=path.parent, suffix='.tmp')
    try:
        with os.fdopen(descriptor, 'w') as handle:
            json.dump(entry, handle, ensure_ascii=False)
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)
    return entry
