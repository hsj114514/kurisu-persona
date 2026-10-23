# SPDX-License-Identifier: GPL-3.0-only
from __future__ import annotations

import hashlib
import json
from pathlib import Path
import shutil
import sys
import tempfile
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta
from unittest.mock import patch
from zipfile import ZipFile

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'pipeline'))
import outreach
import memory
import state
import schedule
import composer
import build_persona
import package_release
import validate_schemas


def write_json(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False), encoding='utf-8')


class LocalTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.p = Path(self.tmp.name)
        # Construct a time guaranteed to be outside quiet hours in this machine's timezone.
        self.now = datetime.now().astimezone().replace(hour=10, minute=5, second=0, microsecond=0)
        self.consent = self.p / 'consent.json'
        self.config = self.p / 'config.json'
        write_json(self.consent, {'subjects': {'local': {'scopes': {'proactive': True}}}})
        self.cfg = {'enabled': True, 'quiet_hours': ['22:00', '09:00'],
                    'daily_cap': 1, 'rolling_24h_cap': 1, 'cooldown_hours': 6,
                    'use_learned_schedule': False}
        write_json(self.config, self.cfg)
        self.e = outreach.LocalOutreach(
            db=self.p/'outreach.sqlite3', config=self.config, inbox=self.p/'inbox.jsonl',
            pending=self.p/'pending', interaction=self.p/'interaction.jsonl',
            consent=self.consent, rules=ROOT/'state/rules.json', schedule=self.p/'schedule.json',
            commitments=self.p/'commitments.jsonl', events=self.p/'events.jsonl',
            notifications=self.p/'notifications.jsonl', notify_enabled=False)

    def planned(self, offset=0):
        task = self.e.add('follow_up', self.now-timedelta(minutes=5+offset))['task_id']
        self.e.tick(self.now)
        return task

    def reserve(self, task):
        with patch.object(self.e, '_deliver_reserved', side_effect=RuntimeError('simulated crash')):
            with self.assertRaises(RuntimeError):
                self.e.accept(task, '虚构测试提醒。', self.now)

    def test_safe_defaults(self):
        rules = state.load_rules()
        self.assertFalse(any(rules['consent']['default'].values()))
        self.assertFalse(json.loads((ROOT/'state/outreach.json').read_text('utf-8'))['enabled'])
        self.assertFalse(outreach.DEFAULT_CONFIG['enabled'])
        self.assertEqual(composer.load_config()['provider'], 'mock')
        self.assertFalse(composer.load_config()['allow_network'])

    def test_model_schema_and_reproducible_card(self):
        model = build_persona.load_json(build_persona.MODEL)
        schema = validate_schemas.load(ROOT/'schema/persona_model.schema.json')
        self.assertEqual(validate_schemas.Validator(schema).errors(model, schema), [])
        card = build_persona.render(model, build_persona.default_state_block())
        self.assertEqual(build_persona.validate(model, card), [])
        self.assertEqual(card, (ROOT/'persona/kurisu.md').read_text('utf-8'))
        self.assertIn('未授权（保持关闭）：长期记忆', card)

    def test_concurrent_same_task_once(self):
        task = self.planned()
        start = threading.Barrier(2)
        def accept():
            start.wait(5)
            try:
                return self.e.accept(task, '虚构并发提醒。', self.now)['status']
            except outreach.Reject:
                return 'rejected'
        with ThreadPoolExecutor(max_workers=2) as pool:
            futures = [pool.submit(accept) for _ in range(2)]
            results = [f.result(timeout=15) for f in futures]
        self.assertEqual(sorted(results), ['delivered', 'rejected'])
        self.assertEqual(len(outreach.read_jsonl(self.e.inbox)), 1)

    def test_different_tasks_do_not_exceed_cap(self):
        first = self.e.add('a', self.now-timedelta(minutes=5))['task_id']
        second = self.e.add('b', self.now-timedelta(minutes=4))['task_id']
        self.e.tick(self.now)
        start = threading.Barrier(2)
        def accept(task):
            start.wait(5)
            try:
                return self.e.accept(task, '虚构额度提醒。', self.now)['status']
            except outreach.Reject:
                return 'rejected'
        with ThreadPoolExecutor(max_workers=2) as pool:
            futures = [pool.submit(accept, t) for t in (first, second)]
            results = [f.result(timeout=15) for f in futures]
        self.assertEqual(results.count('delivered'), 1)
        self.assertEqual(len(outreach.read_jsonl(self.e.inbox)), 1)

    def test_recover_after_revoke_blocked(self):
        task = self.planned()
        self.reserve(task)
        write_json(self.consent, {'subjects': {'local': {'scopes': {'proactive': False}}}})
        self.assertEqual(self.e.recover(self.now)['failed'], [task])
        self.assertFalse(self.e.inbox.exists())
        self.assertEqual(self.e.recover(self.now)['recovered'], [])

    def test_recover_in_quiet_blocked(self):
        task = self.planned()
        self.reserve(task)
        self.assertEqual(self.e.recover(self.now.replace(hour=23))['failed'], [task])
        self.assertFalse(self.e.inbox.exists())

    def test_recover_expired_blocked(self):
        task = self.planned()
        self.reserve(task)
        self.assertEqual(self.e.recover(self.now+timedelta(days=2))['failed'], [task])
        self.assertFalse(self.e.inbox.exists())

    def test_recover_disabled_blocked(self):
        task = self.planned()
        self.reserve(task)
        write_json(self.config, {**self.cfg, 'enabled': False})
        self.assertEqual(self.e.recover(self.now)['failed'], [task])
        self.assertFalse(self.e.inbox.exists())

    def test_recover_quota_rechecked(self):
        task = self.planned()
        self.reserve(task)
        write_json(self.config, {**self.cfg, 'daily_cap': 0})
        self.assertEqual(self.e.recover(self.now)['failed'], [task])
        self.assertFalse(self.e.inbox.exists())

    def test_recover_writes_once(self):
        task = self.planned()
        self.reserve(task)
        self.assertEqual(self.e.recover(self.now)['recovered'], [task])
        self.assertEqual(self.e.recover(self.now)['recovered'], [])
        self.assertEqual(len(outreach.read_jsonl(self.e.inbox)), 1)

    def test_written_record_reconciles_without_resend(self):
        task = self.planned()
        self.reserve(task)
        record = {'task_id': task, 'ts': self.now.isoformat(), 'text': '虚构测试提醒。'}
        self.e.inbox.write_text(json.dumps(record)+'\n', encoding='utf-8')
        write_json(self.consent, {'subjects': {'local': {'scopes': {'proactive': False}}}})
        later = self.now+timedelta(days=2)
        self.assertEqual(self.e.recover(later)['recovered'], [task])
        self.assertEqual(len(outreach.read_jsonl(self.e.inbox)), 1)
        with self.e._conn() as con:
            self.assertEqual(self.e.meta_get(con, 'last_proactive_success_at'), self.now.isoformat())

    def test_accept_checks_time_and_force_consent(self):
        task = self.e.add('future', self.now+timedelta(hours=1))['task_id']
        with self.assertRaises(outreach.Reject):
            self.e.accept(task, '不能提前。', self.now, override=True)
        due = self.planned(1)
        write_json(self.consent, {'subjects': {'local': {'scopes': {'proactive': False}}}})
        with self.assertRaises(outreach.Reject):
            self.e.accept(due, '没有同意。', self.now, override=True)

    def test_cannot_reopen_delivered(self):
        task = self.planned()
        self.e.accept(task, '只写一次。', self.now)
        for force in (False, True):
            with self.assertRaises(outreach.Reject):
                self.e.accept(task, '不能再写。', self.now, override=force)
        self.assertEqual(len(outreach.read_jsonl(self.e.inbox)), 1)

    def test_naive_due_rejected(self):
        with self.assertRaises(outreach.Reject):
            self.e.add('bad', datetime(2030, 1, 1, 10))
        with self.assertRaises(outreach.Reject):
            outreach.now_local('2030-01-01T10:00:00')

    def test_memory_consent_scopes_and_session(self):
        inter, session, tomb = (self.p/n for n in ('interaction.jsonl', 'session.jsonl', 'tomb.jsonl'))
        kwargs = dict(subject='alice', channel='private', consent_path=self.consent,
                      interaction_path=inter, session_path=session, tombstone_path=tomb)
        row = memory.append_interaction('user', '虚构会话事实。', session_id='s1', **kwargs)
        self.assertFalse(row['persist'])
        self.assertIn('expires_at', row)
        self.assertFalse(inter.exists())
        memory.set_consent('alice', 'remember', True, path=self.consent)
        memory.append_interaction('user', '虚构普通事实。', **kwargs)
        memory.append_interaction('user', '虚构敏感事实。', sensitive=True, session_id='s1', **kwargs)
        digest = memory.interaction_digest(path=inter, subject='alice', channel='private', consent_path=self.consent)
        self.assertIn('虚构普通事实', ''.join(digest))
        self.assertNotIn('敏感', ''.join(digest))
        self.assertEqual(memory.interaction_digest(path=inter, subject='bob', consent_path=self.consent), [])
        self.assertEqual(memory.interaction_digest(path=inter, subject='alice', channel='group', consent_path=self.consent), [])
        memory.set_consent('alice', 'remember', False, path=self.consent)
        self.assertEqual(memory.interaction_digest(path=inter, subject='alice', consent_path=self.consent), [])

    def test_state_deduplicates_and_caps(self):
        st = state.RelationshipState(state.load_rules())
        for i in range(8):
            st.apply('promise_kept', event_id=f'e{i}', occurred_at=self.now.isoformat())
        self.assertEqual(st.trust, 13)
        before = st.to_dict()
        self.assertFalse(st.apply('promise_kept', event_id='e0', occurred_at=self.now.isoformat())['applied'])
        self.assertEqual(before, st.to_dict())

    def test_schedule_requires_consent_and_samples(self):
        result = schedule.learn([self.p/'missing'], consent_path=self.consent, rules_path=ROOT/'state/rules.json', now=self.now)
        self.assertEqual(result['state'], 'unauthorized')
        memory.set_consent('local', 'schedule', True, path=self.consent)
        result = schedule.learn([self.p/'missing'], consent_path=self.consent, rules_path=ROOT/'state/rules.json', now=self.now)
        self.assertEqual(result['state'], 'insufficient')
        rows = [{'speaker': 'user', 'subject': 'local', 'text': f'样例{i}',
                 'ts': (self.now-timedelta(days=i//6+1)).isoformat()} for i in range(30)]
        rows += [{'speaker': 'user', 'ts': '2030-01-01T10:00:00', 'text': '无时区'}]
        path = self.p/'sample.jsonl'
        path.write_text(''.join(json.dumps(r)+'\n' for r in rows), encoding='utf-8')
        result = schedule.learn([path], consent_path=self.consent, rules_path=ROOT/'state/rules.json', now=self.now)
        self.assertEqual((result['state'], result['messages'], result['days']), ('ok', 30, 5))

    def test_network_requires_explicit_switch(self):
        with patch('urllib.request.urlopen', side_effect=AssertionError('must not call network')):
            with self.assertRaises(composer.ComposeError):
                composer.call_openai([], {'endpoint': 'https://example.com', 'allow_network': False})

    def test_source_package_excludes_private_data_and_checksums(self):
        names = package_release.source_files()
        sentinel = ('PRIVATE_' + self.p.name + '_DO_NOT_PUBLISH').encode()
        clone = self.p/'copy'
        clone.mkdir()
        for name in names:
            p = clone/name
            p.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(ROOT/name, p)
        for name in ('.env', 'memory/consent.json', 'runtime/inbox.jsonl', 'data/private.jsonl', 'corpus/raw/book.txt', 'SHA256SUMS.txt'):
            p = clone/name
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_bytes(sentinel)
        out = self.p/'source.zip'
        package_release.package(out, clone)
        first = out.read_bytes()
        package_release.package(out, clone)
        self.assertEqual(first, out.read_bytes())
        with ZipFile(out) as z:
            self.assertEqual(len(z.namelist()), len(set(z.namelist())))
            prefix = 'kurisu-persona-v0.1/'
            for name in z.namelist():
                self.assertNotIn(sentinel, z.read(name))
            sums = z.read(prefix+'SHA256SUMS.txt').decode()
            self.assertNotIn('  SHA256SUMS.txt', sums)
            for row in sums.splitlines():
                digest, name = row.split('  ', 1)
                self.assertEqual(hashlib.sha256(z.read(prefix+name)).hexdigest(), digest)


if __name__ == '__main__':
    unittest.main()
