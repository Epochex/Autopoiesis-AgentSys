"""Operator-selected conflict plans never write or silently close an incident."""
import asyncio
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from fastapi import HTTPException
from frontend.gateway.app import change_routes as routes, main
from frontend.gateway.app.device_changes import reservation_cli


def request():
    return SimpleNamespace(method='POST', headers={'x-autopoiesis-console': 'change-control'}, client=SimpleNamespace(host='test'))


@pytest.fixture
def fixture(monkeypatch):
    async def ownership(ip):
        return {'state': {'revision': 3}, 'claimants': [{'mac': '00:11:22:33:44:66', 'retired': False}], 'observation_at': '2026-09-08'}
    monkeypatch.setattr(main, 'rca_ownership', ownership)
    monkeypatch.setattr(main, '_case_repository', lambda: SimpleNamespace(get=lambda _: SimpleNamespace(subject='192.168.1.110')))
    svc = SimpleNamespace(prepare=Mock(return_value={'id': 'plan-one', 'status': 'awaiting_approval'}), store=SimpleNamespace(event=Mock()), execute=Mock())
    monkeypatch.setattr(routes, 'service', lambda: svc)
    return svc


def body(**overrides):
    return routes.IpConflictPrepare(**dict({'ip': '192.168.1.110', 'mac': '00:11:22:33:44:66', 'revision': 3, 'purpose': 'Verified physical device', 'case_id': 'case-one'}, **overrides))


def test_selected_candidate_prepares_review_only(fixture):
    result = asyncio.run(routes.prepare_ip_conflict(body(), request()))
    assert result['status'] == 'awaiting_approval'
    assert fixture.prepare.call_args.args[2] == {'ip': '192.168.1.110', 'mac': '00:11:22:33:44:66'}
    assert fixture.prepare.call_args.args[-1] == 'case-one'
    fixture.execute.assert_not_called()


@pytest.mark.parametrize('changes', [{'revision': 2}, {'mac': '00:11:22:33:44:77'}])
def test_changed_assignment_or_unknown_candidate_blocks_prepare(fixture, changes):
    with pytest.raises(HTTPException) as exc:
        asyncio.run(routes.prepare_ip_conflict(body(**changes), request()))
    assert exc.value.status_code == 409
    fixture.prepare.assert_not_called()


def test_unrelated_case_cannot_receive_plan(fixture, monkeypatch):
    monkeypatch.setattr(main, '_case_repository', lambda: SimpleNamespace(get=lambda _: SimpleNamespace(subject='192.168.1.17')))
    with pytest.raises(HTTPException):
        asyncio.run(routes.prepare_ip_conflict(body(), request()))
    fixture.prepare.assert_not_called()


def test_dhcp_success_requires_followup_conflict_observation(fixture, monkeypatch):
    repository = SimpleNamespace(append_event=Mock())
    monkeypatch.setattr(main, '_case_repository', lambda: repository)
    result = {'id': 'plan-one', 'case_id': 'case-one', 'action': 'dhcp_reserve', 'target': {}, 'status': 'succeeded'}
    routes.background(lambda *_: result, 'plan-one', 'tester')
    assert repository.append_event.call_args.kwargs['status'] == 'waiting'


def test_cli_reflects_actual_ids_and_has_inverse():
    commands = reservation_cli(9, 27, {'type': 'mac', 'ip': '192.168.1.110', 'mac': '00:11:22:33:44:66', 'action': 'reserved', 'description': 'reviewed'})
    assert 'edit 9' in commands and 'edit 27' in commands
    assert 'set mac "00:11:22:33:44:66"' in commands
    assert 'delete 27' in reservation_cli(9, 27, None)
    restore = reservation_cli(9, 27, {'type': 'mac', 'ip': '192.168.1.110', 'mac': '00:11:22:33:44:55', 'action': 'reserved', 'description': None})
    assert 'set mac "00:11:22:33:44:55"' in restore
    assert 'unset description' in restore
