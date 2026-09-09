"""Production reads do not spend tokens; refreshes reuse relevant evidence."""
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
import json
import threading
import time

import pytest

from frontend.gateway.app import production_threat as threat
from frontend.gateway.app.analysis_cache import cache_paths, stable_evidence


def observation():
    return {'observedAt': '2026-09-08T08:00:00Z', 'freshness': {'ageSec': 1},
            'externalSources': [{'ip': '87.251.64.127', 'events': 1343,
                                 'eventTypes': ['admin_login_failed'], 'lastSeenAt': '08:00:00'}],
            'inventory': {'assets': [{'ip': '192.168.1.10', 'segment': '192.168.1.0/24'}]}}


@pytest.fixture
def calls(monkeypatch, tmp_path):
    monkeypatch.setenv('AUTOPOIESIS_LLM_CACHE_DIR', str(tmp_path))
    monkeypatch.setattr(threat, 'model_name', lambda: 'cache-test')
    calls = []
    def generate(*args):
        calls.append(args)
        return {'ok': True, 'verdict': 'compromised', 'severity': 'critical',
                'campaign': 'Suspected campaign', 'killChain': 'lateral-movement'}
    monkeypatch.setattr(threat, '_generate_production_source', generate)
    return calls


def test_read_returns_immediate_facts_without_model(calls):
    result = threat.assess_production_source('87.251.64.127', observation(), mode='read')
    assert calls == []
    assert result['eventCount'] == 1343 and result['analysisState'] == 'baseline'
    assert result['cache']['status'] == 'miss' and result['severity'] == 'medium'
    assert result['riskAssessment']['successfulIntrusion'] is False


def test_persisted_cache_reuses_refresh_timestamps_and_small_count_growth(calls):
    first = threat.assess_production_source('87.251.64.127', observation(), mode='analyze')
    changed = observation()
    changed['observedAt'] = '2026-09-08T08:00:05Z'
    changed['externalSources'][0].update(lastSeenAt='08:00:05', events=1344)
    changed['freshness']['ageSec'] = 0
    cached = threat.assess_production_source('87.251.64.127', changed, mode='analyze')
    assert len(calls) == 1
    assert first['cache']['status'] == 'generated' and cached['cache']['status'] == 'hit'
    assert cached['cache']['analyzedAt'] == first['cache']['analyzedAt']
    assert cached['eventCount'] == 1344 and cached['observedAt'] == changed['observedAt']
    assert cached['severity'] == 'medium' and cached['killChain'] == 'credential-access'
    assert cached['modelInference']['verdict'] == 'compromised'
    assert cached['verdict'] != 'compromised'


@pytest.mark.parametrize('mutation', ['event_type', 'growth', 'port', 'confirmation'])
def test_material_evidence_change_stales_without_automatic_spend(calls, mutation):
    threat.assess_production_source('87.251.64.127', observation(), mode='analyze')
    changed = observation()
    row = changed['externalSources'][0]
    if mutation == 'event_type':
        row['eventTypes'].append('admin_login_success')
    elif mutation == 'growth':
        row['events'] = 9000
    elif mutation == 'port':
        row['ports'] = [443]
    else:
        row.update(confirmedCompromise=True, evidenceRefs=['verified-123'])
    result = threat.assess_production_source('87.251.64.127', changed, mode='read')
    assert len(calls) == 1 and result['analysisState'] == 'baseline'
    assert result['cache']['status'] == 'stale' and result['cache']['evidenceChanged']
    threat.assess_production_source('87.251.64.127', changed, mode='analyze')
    assert len(calls) == 2


def test_expiry_and_force_are_explicit(calls):
    threat.assess_production_source('87.251.64.127', observation(), mode='analyze')
    path, _ = cache_paths(['source', '87.251.64.127', 'zh', 'cache-test'])
    entry = json.loads(path.read_text())
    entry['expires'] = 1
    path.write_text(json.dumps(entry))
    result = threat.assess_production_source('87.251.64.127', observation(), mode='read', force=True)
    assert result['cache']['status'] == 'stale' and len(calls) == 1
    threat.assess_production_source('87.251.64.127', observation(), mode='analyze')
    threat.assess_production_source('87.251.64.127', observation(), mode='analyze', force=True)
    assert len(calls) == 3


def test_concurrent_requests_share_single_model_call(calls, monkeypatch):
    gate = threading.Barrier(4)
    def generate(*args):
        calls.append(args)
        time.sleep(.08)
        return {'ok': True, 'verdict': 'attempt', 'severity': 'medium'}
    monkeypatch.setattr(threat, '_generate_production_source', generate)
    def request(_):
        gate.wait()
        return threat.assess_production_source('87.251.64.127', observation(), mode='analyze')
    with ThreadPoolExecutor(max_workers=4) as pool:
        results = list(pool.map(request, range(4)))
    assert len(calls) == 1
    assert sorted(r['cache']['status'] for r in results) == ['generated', 'hit', 'hit', 'hit']


def test_failure_keeps_baseline_and_does_not_cache_failure(calls, monkeypatch):
    monkeypatch.setattr(threat, '_generate_production_source', lambda *a: {'ok': False, 'text': 'disabled'})
    result = threat.assess_production_source('87.251.64.127', observation(), mode='analyze')
    assert result['ok'] and result['eventCount'] == 1343 and result['analysisError'] == 'disabled'
    assert result['cache']['status'] == 'miss'


def test_high_requires_traceable_confirmation_and_blocked_is_observed_not_assumed(calls):
    data = observation()
    data['externalSources'][0].update(confirmedCompromise=True, severity='critical')
    result = threat.assess_production_source('87.251.64.127', data, mode='read')
    assert result['severity'] == 'medium' and not result['riskAssessment']['blocked']
    data['externalSources'][0]['evidenceRefs'] = ['verified-123']
    assert threat.assess_production_source('87.251.64.127', data, mode='read')['severity'] == 'high'
    data['externalSources'][0].update(confirmedCompromise=False, action='deny')
    result = threat.assess_production_source('87.251.64.127', data, mode='read')
    assert result['severity'] == 'low' and result['riskAssessment']['blocked']


def test_scope_reads_and_cache_isolate_language_and_subject(calls, monkeypatch):
    def scope(*args):
        calls.append(args)
        return {'ok': True, 'analysis': 'Details', 'summary': 'Summary'}
    monkeypatch.setattr(threat, '_generate_production_scope', scope)
    for scope_name, subject in [('host', '192.168.1.10'), ('subnet', '192.168.1.0/24')]:
        assert threat.assess_production_scope(subject, scope_name, observation(), mode='read')['analysisState'] == 'baseline'
        threat.assess_production_scope(subject, scope_name, observation(), mode='analyze')
        threat.assess_production_scope(subject, scope_name, observation(), mode='analyze')
        threat.assess_production_scope(subject, scope_name, observation(), lang='en', mode='analyze')
    assert len(calls) == 4


def test_stable_evidence_preserves_new_single_event():
    assert stable_evidence({'events': 0}) != stable_evidence({'events': 1})
    assert stable_evidence({'rows': [{'a': 1}, {'a': 2}]}) == stable_evidence({'rows': [{'a': 2}, {'a': 1}]})
