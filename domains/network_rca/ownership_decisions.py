"""Operator address assignments. Observations remain append-only and independent."""
from __future__ import annotations

import ipaddress
import json
import os
from pathlib import Path
import re
import sqlite3
from datetime import datetime, timezone


def ledger_path() -> Path:
    return Path(os.getenv('AUTOPOIESIS_OWNERSHIP_DB', '/data/autopoiesis-production/ownership-decisions.sqlite3'))


def read_decisions(ip: str | None = None) -> list[dict]:
    path = ledger_path()
    if not path.exists():
        return []
    with sqlite3.connect(f'file:{path}?mode=ro', uri=True) as db:
        query = 'SELECT payload FROM decisions'
        rows = db.execute(query + (' WHERE ip=?' if ip else '') + ' ORDER BY seq', (ip,) if ip else ())
        return [json.loads(row[0]) for row in rows]


def current_state(events: list[dict]) -> dict:
    state = {'revision': 0, 'expected_mac': None, 'retired_macs': [], 'decided_at': None, 'device_names': {}}
    for event in events:
        state['revision'] = event['revision']
        if event.get('device_name'):
            state['device_names'][event['mac']] = event['device_name']
        if event['action'] == 'set_owner':
            state['expected_mac'] = event['mac']
            state['decided_at'] = event['at']
            state['retired_macs'] = [m for m in state['retired_macs'] if m != event['mac']]
        elif event['action'] == 'clear_owner':
            state['expected_mac'] = None
            state['decided_at'] = event['at']
        elif event['action'] == 'retire_record' and event['mac'] not in state['retired_macs']:
            state['retired_macs'].append(event['mac'])
    return state


def save_decision(ip: str, mac: str, action: str, revision: int, reason: str, operator: str, device_name: str = '') -> dict:
    ip = str(ipaddress.IPv4Address(ip))
    mac = mac.lower()
    if not re.fullmatch(r'(?:[0-9a-f]{2}:){5}[0-9a-f]{2}', mac):
        raise ValueError('invalid MAC')
    if action not in {'set_owner', 'retire_record', 'clear_owner', 'label_device'} or not reason.strip() or not operator.strip():
        raise ValueError('action, reason and operator are required')
    if action == 'label_device' and not device_name.strip():
        raise ValueError('device_name_required')
    path = ledger_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(path, timeout=10) as db:
        db.execute('CREATE TABLE IF NOT EXISTS decisions(seq INTEGER PRIMARY KEY, ip TEXT NOT NULL, payload TEXT NOT NULL)')
        db.execute('BEGIN IMMEDIATE')
        events = [json.loads(row[0]) for row in db.execute('SELECT payload FROM decisions WHERE ip=? ORDER BY seq', (ip,))]
        state = current_state(events)
        if revision != state['revision']:
            raise ValueError('revision_conflict')
        if action in {'retire_record', 'clear_owner'} and state['expected_mac'] == mac and action == 'retire_record':
            raise ValueError('clear_assignment_before_retiring')
        if action == 'clear_owner' and state['expected_mac'] != mac:
            raise ValueError('assignment_changed')
        event = {'ip': ip, 'mac': mac, 'action': action, 'revision': revision + 1,
                 'reason': reason.strip(), 'operator_label': operator.strip(),
                 'device_name': device_name.strip(),
                 'at': datetime.now(timezone.utc).isoformat(), 'previous_mac': state['expected_mac'],
                 'scope': 'inventory_assignment', 'device_configuration_changed': False}
        db.execute('INSERT INTO decisions(ip,payload) VALUES (?,?)', (ip, json.dumps(event)))
    return event


def assess_assignment(state: dict, observed_macs: list[str], observed_at: str | None, stale: bool) -> str:
    if not state['expected_mac']:
        return 'unassigned'
    if stale or not observed_at or not observed_macs:
        return 'awaiting_observation'
    try:
        observed = datetime.fromisoformat(observed_at.replace('Z', '+00:00'))
        decided = datetime.fromisoformat(state['decided_at'].replace('Z', '+00:00'))
        if observed <= decided:
            return 'awaiting_observation'
    except (ValueError, TypeError):
        return 'awaiting_observation'
    return 'matches_latest_observation' if set(observed_macs) == {state['expected_mac']} else 'conflict_observed'
