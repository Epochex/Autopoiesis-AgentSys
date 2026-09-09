from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import Mock

from frontend.gateway.app import change_routes as routes, investigate


def test_exact_fresh_registered_service_generates_one_plan(monkeypatch):
    s = SimpleNamespace(case_id='case-one', auto_started=True, scope_quality='exact',
        opened_at=datetime.now(timezone.utc).isoformat(), subject='test.service',
        hypothesis_state={'hypotheses':[{'hypothesis_id':'service_failed','status':'confirmed','supporting_evidence_ids':['ev-1']}]},
        evidence=[{'ok':True,'command':'hostname','output':'testhost\n'}])
    monkeypatch.setattr(investigate,'get',lambda _:s)
    monkeypatch.setattr(routes,'policy',lambda:{'hosts':[{'id':'local','units':['test.service']}]})
    monkeypatch.setattr(routes,'HostAdapter',lambda _:SimpleNamespace(identify=lambda:{'hostname':'testhost'}))
    plans=[]
    def prepare(*args):
        p={'id':'p1','case_id':args[-1],'actor':args[-3]}
        plans.append(p)
        return p
    svc=SimpleNamespace(store=SimpleNamespace(list=lambda:plans,event=Mock()),prepare=Mock(side_effect=prepare))
    monkeypatch.setattr(routes,'service',lambda:svc)
    assert routes.propose_investigation_change('s')['id']=='p1'
    assert routes.propose_investigation_change('s')['id']=='p1'
    assert svc.prepare.call_count==1
    s.evidence=[]
    assert routes.propose_investigation_change('s') is None
    s.auto_started=False
    assert routes.propose_investigation_change('s') is None


def test_automatic_worker_respects_pause_and_skips_manual_plans(monkeypatch):
    plans=[{'id':'auto','actor':'system:investigation','status':'approved'},
           {'id':'manual','actor':'console:test','status':'approved'}]
    svc=SimpleNamespace(store=SimpleNamespace(list=lambda:plans),paused=lambda:True,execute=Mock())
    background=Mock()
    monkeypatch.setattr(routes,'background',background)
    routes.advance_automatic_changes(svc)
    background.assert_not_called()
    svc.paused=lambda:False
    routes.advance_automatic_changes(svc)
    background.assert_called_once_with(svc.execute,'auto','system:approved-change')


def test_model_input_snapshot_preserves_actual_messages(monkeypatch):
    trace=Mock(); save=Mock()
    monkeypatch.setattr(investigate,'_persist_session_trace',trace)
    monkeypatch.setattr(investigate,'_persist_session',save)
    s=SimpleNamespace(session_id='session',working_set=None,retrieval_results=[
        {'item_id':'kept','selected_for_context':True},{'item_id':'dropped','selected_for_context':False}])
    messages=[{'role':'user','content':'exact collected evidence'}]
    investigate._record_model_context(s,messages,'rca_analysis')
    p=trace.call_args.args[2]
    assert p['messages']==messages
    assert [r['item_id'] for r in p['retrieval_results']]==['kept']
    save.assert_called_once_with(s)
