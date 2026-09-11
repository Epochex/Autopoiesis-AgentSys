"""Local console confirmation surface and background change observation."""
from __future__ import annotations

import asyncio
import json
import os
import threading
from pathlib import Path
from urllib.parse import urlsplit

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, Field

from core.remediate.change_control import ChangeError, ChangeService, ChangeStore
from .device_changes import CATALOG, FortiAdapter
from .host_changes import HostAdapter
from .live_identity import _creds

router = APIRouter(prefix='/api/rca/changes', tags=['approved device changes'])
_service = None
_lock = threading.Lock()


def policy():
    path = Path(os.getenv('AUTOPOIESIS_DEVICE_CHANGE_POLICY', '/data/autopoiesis-production/device-change-policy.json'))
    if not path.exists():
        return {'fortigate': {'vdom': 'root'}, 'hosts': [], 'preauthorizations': []}
    if path.stat().st_mode & 0o022:
        raise ChangeError('device_policy_must_not_be_group_or_world_writable')
    return json.loads(path.read_text())


def adapter_for(device_id):
    config = policy()
    if device_id == 'fortigate':
        return FortiAdapter(policy=config.get('fortigate', {}))
    host = next((h for h in config.get('hosts', []) if h['id'] == device_id), None)
    if host:
        return HostAdapter(host)
    raise ChangeError('device_not_registered')


def service():
    global _service
    with _lock:
        if _service is None:
            store = ChangeStore(os.getenv('AUTOPOIESIS_CHANGE_DB', '/data/autopoiesis-production/device-changes.sqlite3'))
            _service = ChangeService(store, adapter_for, paused=writes_paused)
        return _service


def write_control():
    from core.remediate.safety import EmergencyStop
    return EmergencyStop(os.getenv('AUTOPOIESIS_CHANGE_STOP', '/data/autopoiesis-production/device-changes-stop.json'))


def writes_paused():
    from .remediation import emergency_stop
    legacy = emergency_stop()
    # A separately commissioned approved-change channel does not enable Sentinel.
    # An explicit global pause still stops both channels.
    return write_control().paused or (legacy.path.exists() and legacy.paused)


@router.get('/control')
def control(request: Request):
    actor(request)
    return {'paused': writes_paused(), 'channel': write_control().status().to_dict()}


class Control(BaseModel):
    paused: bool
    reason: str = Field(default='operator_changed_write_channel', min_length=3, max_length=1000)


@router.post('/control')
def set_control(body: Control, request: Request):
    who = actor(request)
    if body.paused:
        write_control().pause(body.reason, who)
    else:
        write_control().resume(who, body.reason)
    service().store.event('control', 'write_channel_changed', {'actor': who, **body.model_dump()})
    return {'paused': writes_paused(), 'channel': write_control().status().to_dict()}


def same_origin(request):
    origin = request.headers.get('origin')
    if origin and urlsplit(origin).netloc != request.headers.get('host'):
        raise HTTPException(403, 'cross_origin_write_rejected')
    if request.headers.get('sec-fetch-site') == 'cross-site':
        raise HTTPException(403, 'cross_site_write_rejected')


def actor(request):
    same_origin(request)
    # A custom header prevents browser forms from submitting writes. This is
    # CSRF protection, not identity authentication. Audit the direct peer only.
    if request.method not in ('GET', 'HEAD') and request.headers.get('x-autopoiesis-console') != 'change-control':
        raise HTTPException(403, 'console_write_header_required')
    return 'console:' + (request.client.host if request.client else 'unknown')


@router.get('/catalog')
def catalog():
    config = policy()
    devices = [{'id': 'fortigate', 'label': 'FortiGate', 'kind': 'fortigate', 'configured': bool(_creds())}]
    devices += [{'id': h['id'], 'label': h.get('label', h['id']), 'kind': 'linux', 'configured': True} for h in config.get('hosts', [])]
    return {'devices': devices, 'actions': [{'id': k, 'label': v[0], 'target_fields': v[1], 'parameter_fields': v[2],
        'category': v[3], 'reversible': v[4], 'kind': 'linux' if k.startswith('host_') or k in ('restart_unit', 'bounce_interface') else 'fortigate'} for k, v in CATALOG.items()],
        'approval_required': True, 'preauthorization_rule_count': len(config.get('preauthorizations', []))}


@router.get('/assessment')
def assessment(request: Request, case_id: str = ''):
    actor(request)
    from .investigate import _session_store
    plans = [p for p in service().store.list() if not case_id or p.get('case_id') == case_id]
    pending = [p for p in plans if p['status'] in ('awaiting_approval', 'approved', 'executing', 'observing', 'restoring')]
    # One indexed lookup instead of parsing the thousand newest snapshots per poll.
    session = _session_store().find_latest(subject='', incident_ref=case_id, by_source_refs=False) if case_id else None
    return {'pending_count': len(pending), 'paused': writes_paused(),
            'decision': (session or {}).get('decision', {}),
            'session_id': (session or {}).get('session_id'),
            'headline': '已有处置方案，系统持续跟踪执行结果' if pending else '当前没有待执行的配置变更',
            'automatic_scope': '自动调查生成已登记目标的恢复方案；匹配预授权后执行并持续回读。'}


_proposal_lock = threading.Lock()


def propose_investigation_change(session_id):
    """Turn a fresh, exact local service diagnosis into one durable preview."""
    from .investigate import get
    from datetime import datetime, timezone
    s = get(session_id)
    if not s.case_id or not s.auto_started or s.scope_quality != 'exact':
        return None
    age = (datetime.now(timezone.utc) - datetime.fromisoformat(s.opened_at)).total_seconds()
    if age < 0 or age > 300:
        return None
    hypotheses = s.hypothesis_state.get('hypotheses', [])
    confirmed = [h for h in hypotheses if h.get('status') == 'confirmed']
    if len(confirmed) != 1 or confirmed[0].get('hypothesis_id') != 'service_failed' or any(h.get('status') in ('proposed', 'testing') for h in hypotheses):
        return None
    hosts = [h for h in policy().get('hosts', []) if not h.get('ssh') and s.subject in h.get('units', [])]
    if len(hosts) != 1:
        return None
    host = hosts[0]
    identity = HostAdapter(host).identify()
    if not any(e.get('ok') and e.get('command') == 'hostname' and str(e.get('output', '')).strip() == identity['hostname'] for e in s.evidence):
        return None
    with _proposal_lock:
        existing = next((p for p in service().store.list() if p.get('case_id') == s.case_id and p.get('actor') == 'system:investigation'), None)
        if existing:
            return existing
        p = service().prepare(host['id'], 'restart_unit', {'device': host['id'], 'unit': s.subject}, {},
                              'system:investigation', '现场证据确认服务失败，恢复该服务并检查业务与管理连接。', s.case_id)
        service().store.event(p['id'], 'investigation_plan_created', {'session_id': session_id, 'evidence_ids': confirmed[0].get('supporting_evidence_ids', [])})
        return p


@router.get('/devices/{device_id}')
def device(device_id: str, request: Request):
    actor(request)
    a = adapter_for(device_id)
    try:
        return a.inventory() if hasattr(a, 'inventory') else {'identity': a.identify()}
    finally:
        if hasattr(a, 'close'):
            a.close()


class Prepare(BaseModel):
    device_id: str = Field(min_length=1, max_length=100)
    action: str = Field(min_length=1, max_length=100)
    target: dict = Field(default_factory=dict)
    params: dict = Field(default_factory=dict)
    purpose: str = Field(min_length=3, max_length=1000)
    case_id: str = Field(default='', max_length=120)


async def guarded(fn, *args):
    try:
        return await asyncio.to_thread(fn, *args)
    except (ChangeError, KeyError, TypeError, ValueError) as exc:
        raise HTTPException(409, str(exc)[:500]) from exc


@router.post('')
async def prepare(body: Prepare, request: Request):
    who = actor(request)
    if len(json.dumps(body.model_dump())) > 12000:
        raise HTTPException(413, 'change_too_large')
    if body.case_id:
        from .main import _case_repository
        if _case_repository().get(body.case_id) is None:
            raise HTTPException(404, 'investigation_case_not_found')
    return await guarded(service().prepare, body.device_id, body.action, body.target, body.params, who, body.purpose, body.case_id)


@router.get('')
def changes(request: Request, case_id: str | None = None):
    actor(request)
    rows = service().store.list()
    return {'changes': [r for r in rows if not case_id or r.get('case_id') == case_id]}


class IpConflictPrepare(BaseModel):
    ip: str = Field(min_length=7, max_length=15)
    mac: str = Field(min_length=17, max_length=17)
    revision: int = Field(ge=0)
    purpose: str = Field(min_length=3, max_length=1000)
    case_id: str = Field(default='', max_length=120)


@router.get('/ip-conflict')
async def ip_conflict(request: Request, ip: str):
    actor(request)
    from .main import rca_ownership
    from .device_changes import ip4
    return await rca_ownership(ip4(ip))


@router.post('/ip-conflict/prepare')
async def prepare_ip_conflict(body: IpConflictPrepare, request: Request):
    who = actor(request)
    from .main import rca_ownership, _case_repository
    from .device_changes import ip4, mac_address
    ip, mac = ip4(body.ip), mac_address(body.mac)
    view = await rca_ownership(ip)
    if body.revision != view['state']['revision']:
        raise HTTPException(409, 'ownership_revision_changed_refresh_required')
    if mac not in {c['mac'] for c in view['claimants'] if not c.get('retired')}:
        raise HTTPException(409, 'select_an_observed_active_claimant')
    if body.case_id:
        case = _case_repository().get(body.case_id)
        if case is None:
            raise HTTPException(404, 'investigation_case_not_found')
        if case.subject != ip:
            raise HTTPException(409, 'case_subject_does_not_match_conflict_address')
    result = await guarded(service().prepare, 'fortigate', 'dhcp_reserve',
                           {'ip': ip, 'mac': mac}, {}, who, body.purpose, body.case_id)
    service().store.event(result['id'], 'ownership_candidate_selected',
        {'ip': ip, 'mac': mac, 'ownership_revision': body.revision,
         'observation_at': view.get('observation_at'), 'scope': 'dhcp_reservation'})
    return result


@router.get('/{cid}')
def detail(cid: str, request: Request):
    actor(request)
    try:
        return service().store.get(cid)
    except ChangeError as exc:
        raise HTTPException(404, str(exc)) from exc


class Approval(BaseModel):
    digest: str = Field(min_length=64, max_length=64)
    reason: str = Field(min_length=3, max_length=1000)


@router.post('/{cid}/approve')
async def approve(cid: str, body: Approval, request: Request):
    return await guarded(service().approve, cid, body.digest, actor(request), body.reason)


@router.post('/{cid}/preauthorize')
async def preauthorize(cid: str, request: Request):
    actor(request)
    return await guarded(service().preauthorize, cid, policy().get('preauthorizations', []))


def background(fn, cid, who):
    try:
        result = fn(cid, who)
        if result.get('case_id'):
            from .main import _case_repository
            _case_repository().append_event(result['case_id'], kind='device_change_completed',
                payload={'change_id': cid, 'action': result['action'], 'target': result['target'],
                         'status': result['status'], 'readback': result.get('readback'),
                         'receipt_url': '/api/rca/changes/' + cid},
                status='resolved' if result['status'] == 'succeeded' and result['action'] not in ('dhcp_reserve', 'dhcp_unreserve', 'dhcp_clear_lease', 'arp_clear') else 'escalated' if result['status'] in ('needs_human', 'rolled_back') else 'waiting',
                event_id='device-change:' + cid + ':' + result['status'])
    except Exception as exc:
        service().store.event(cid, 'worker_error', {'type': type(exc).__name__, 'error': str(exc)[:500]})


@router.post('/{cid}/execute')
async def execute(cid: str, request: Request):
    who = actor(request)
    p = await guarded(service().store.get, cid)
    if p['status'] != 'approved':
        raise HTTPException(409, 'approved_change_required')
    if service().paused():
        raise HTTPException(409, 'approved_device_writes_are_paused')
    threading.Thread(target=background, args=(service().execute, cid, who), daemon=True).start()
    return {'id': cid, 'accepted': True, 'message': '通过最终配置核对后执行，回执持续更新'}


@router.post('/{cid}/restore')
async def restore(cid: str, body: Approval, request: Request):
    who = actor(request)
    p = await guarded(service().store.get, cid)
    if body.digest != p['digest'] or p['status'] != 'succeeded' or not p['plan'].get('manual_restore'):
        raise HTTPException(409, 'restorable_change_and_original_digest_required')
    service().store.event(cid, 'restore_approved', {'actor': who, 'reason': body.reason, 'digest': body.digest})
    threading.Thread(target=background, args=(service().restore, cid, who), daemon=True).start()
    return {'id': cid, 'accepted': True}


@router.post('/{cid}/abort')
async def abort(cid: str, body: Approval, request: Request):
    who = actor(request)
    p = await guarded(service().store.get, cid)
    if body.digest != p['digest']:
        raise HTTPException(409, 'digest_mismatch')
    return await guarded(service().store.transition, cid, ('executing', 'observing'), p['status'],
                         {'abort_requested': True, 'abort_actor': who, 'abort_reason': body.reason})


@router.post('/{cid}/cancel')
async def cancel(cid: str, body: Approval, request: Request):
    who = actor(request)
    p = await guarded(service().store.get, cid)
    if body.digest != p['digest']:
        raise HTTPException(409, 'digest_mismatch')
    return await guarded(service().store.transition, cid, ('awaiting_approval', 'approved'), 'cancelled', {'actor': who, 'reason': body.reason})


def advance_automatic_changes(svc):
    for p in svc.store.list():
        if p.get('actor') != 'system:investigation':
            continue
        if p['status'] == 'awaiting_approval':
            try:
                p = svc.preauthorize(p['id'], policy().get('preauthorizations', []))
            except ChangeError:
                continue
        if p['status'] == 'approved' and not svc.paused():
            background(svc.execute, p['id'], 'system:approved-change')


def start_worker():
    stop = threading.Event()
    svc = service()
    svc.reconcile_interrupted()
    def run():
        while not stop.wait(5):
            try:
                svc.expire_temporary_changes()
                advance_automatic_changes(svc)
            except Exception:
                # Durable failed restores are exposed on their change; later expiries keep running.
                pass
    threading.Thread(target=run, name='device-change-expiry', daemon=True).start()
    return stop
