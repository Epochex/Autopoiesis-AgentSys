from types import SimpleNamespace
import pytest
from frontend.gateway.app import display_translation as translation
from frontend.gateway.app import investigate


def test_translation_deduplicates_and_preserves_identifiers(monkeypatch):
    saved = {}
    calls = []
    monkeypatch.setattr(translation.model_access, 'cache_get', lambda ns, key: saved.get(key))
    monkeypatch.setattr(translation.model_access, 'cache_put', lambda ns, key, value: saved.update({key: value}))
    def complete(messages, **kwargs):
        calls.append(messages)
        return {'translations': ['Investigate 192.168.1.1 and case-abc']}
    monkeypatch.setattr(translation.model_access, 'client_for', lambda *a, **kw: SimpleNamespace(complete_json=complete))
    source = '调查 192.168.1.1 和 case-abc'
    assert translation.translate_texts([source, source, 'English']) == {source: 'Investigate 192.168.1.1 and case-abc', 'English': 'English'}
    assert translation.translate_texts([source])[source].startswith('Investigate')
    assert len(calls) == 1


def test_rejects_translation_that_changes_identifier(monkeypatch):
    monkeypatch.setattr(translation.model_access, 'cache_get', lambda *a: None)
    monkeypatch.setattr(translation.model_access, 'client_for', lambda *a, **kw: SimpleNamespace(complete_json=lambda *a, **kw: {'translations': ['Investigate 192.168.1.2']}))
    with pytest.raises(RuntimeError, match='identifier'):
        translation.translate_texts(['调查 192.168.1.1'])


def test_retrieval_scoped_to_exact_case_with_subject_alias(monkeypatch):
    snapshots = [dict(session_id='one', case_id='case-a', subject='192.168.1.1', question='A', retrieval_results=[{'item_id': 'doc-a', 'summary': 'A'}]), dict(session_id='two', case_id='case-b', subject='192.168.1.110', question='B', retrieval_results=[{'item_id': 'doc-b', 'summary': 'B'}])]
    monkeypatch.setattr(investigate, '_session_store', lambda: SimpleNamespace(recent=lambda **kwargs: snapshots))
    monkeypatch.setattr(investigate, '_SESSIONS', {})
    payload = investigate.live_retrieval_trace(case_id='case-a', subject='FG100ETK20014183')
    assert [item['case_id'] for item in payload['cases']] == ['case-a']
    assert investigate.live_retrieval_trace(case_id='absent')['cases'] == []
    assert [item['case_id'] for item in investigate.live_retrieval_trace(subject='192.168.1.110')['cases']] == ['case-b']


def test_concurrent_identical_requests_share_inflight_translation(monkeypatch):
    import threading
    from concurrent.futures import ThreadPoolExecutor
    entered, release = threading.Event(), threading.Event()
    calls = []
    monkeypatch.setattr(translation.model_access, 'cache_get', lambda *a: None)
    monkeypatch.setattr(translation.model_access, 'cache_put', lambda *a: None)
    def complete(*args, **kwargs):
        calls.append(1); entered.set(); release.wait(3)
        return {'translations': ['Investigate the firewall']}
    monkeypatch.setattr(translation.model_access, 'client_for', lambda *a, **kw: SimpleNamespace(complete_json=complete))
    with ThreadPoolExecutor(max_workers=2) as pool:
        one = pool.submit(translation.translate_texts, ['调查防火墙'])
        assert entered.wait(2)
        two = pool.submit(translation.translate_texts, ['调查防火墙'])
        # An unrelated English payload never waits on the in-flight model call.
        assert translation.translate_texts(['English']) == {'English': 'English'}
        release.set()
        assert one.result() == two.result()
    assert len(calls) == 1
