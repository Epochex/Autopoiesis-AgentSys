#!/usr/bin/env python3
"""Prepare one controlled failed service for the frontend approval workflow.

No router configuration is changed. Run --cleanup after the observation ends.
"""
import argparse
import json
from pathlib import Path

from demo_support import install_demo_unit, remove_demo_unit, run_command, wait_unit_state

UNIT = 'autopoiesis-acceptance-change.service'
MARKER = Path('/run/autopoiesis-acceptance-change-once')
UNIT_PATH = Path('/run/systemd/system') / UNIT


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--cleanup', action='store_true')
    args = parser.parse_args()
    if args.cleanup:
        if UNIT_PATH.exists() and 'Autopoiesis approved change acceptance' not in UNIT_PATH.read_text():
            raise SystemExit('Refusing to remove an unrelated unit')
        remove_demo_unit(UNIT, UNIT_PATH, MARKER)
        print('Removed the controlled acceptance unit and marker; change receipts retained.')
        return
    if UNIT_PATH.exists() or MARKER.exists():
        raise SystemExit('Acceptance target already exists; inspect it before cleanup.')
    code = f"import pathlib,sys,time; p=pathlib.Path('{MARKER}'); existed=p.exists(); p.touch(); sys.exit(1) if not existed else time.sleep(3600)"
    install_demo_unit(UNIT, '[Unit]\nDescription=Autopoiesis approved change acceptance\n[Service]\nType=simple\nRestart=no\nExecStart=/usr/bin/python3 -c "' + code + '"\n')
    run_command(['systemctl', 'start', UNIT], check=False)
    wait_unit_state(UNIT, 'failed')
    print(json.dumps({'device_id': 'local', 'action': 'restart_unit', 'target': {'device': 'local', 'unit': UNIT},
                      'purpose': '受控验收：批准重启专用失败服务，观察真实服务状态和管理连接。',
                      'entry': '证据与处置 → 处置记录 → 配置变更与审批',
                      'cleanup': './frontend/.venv/bin/python scripts/demo_approved_change.py --cleanup'}, ensure_ascii=False))


if __name__ == '__main__':
    main()
