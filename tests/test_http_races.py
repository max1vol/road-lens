"""Opt-in race tests against a disposable, loopback-only RoadLens Site.

These are infrastructure fixtures, not AI evaluations or physical box tests.
The result callback contains a strictly typed, explicitly synthetic AnalysisResult
with no metrics, no retrieved records and no claimed model execution.

Required environment:
  ROADLENS_TEST_ISOLATED=1
  ROADLENS_TEST_ORIGIN=http://127.0.0.1:<local-preview-port>
  ROADLENS_TEST_DB=<migrated EMPTY disposable D1 SQLite file used by that preview>
  ROADLENS_TEST_DEVICE_TOKEN=<matching private local-test device token>
  ROADLENS_TEST_INTERNAL_TOKEN=<matching private local-test worker token>

Optional: ROADLENS_TEST_PROJECT_ROOT, ROADLENS_TEST_DEVICE_ID (default aiy-box-1),
ROADLENS_TEST_RACE_ROUNDS (1..4, default 3), ROADLENS_TEST_RESULT_PATH.
Configure the preview with Modal disabled or a harmless local fake /wake endpoint;
do not run a background worker. This suite never calls prepare or a real model.
It never clears data. Seeded rows remain for inspection, so use a fresh disposable
database for every suite run. Tokens are read from the environment, never printed.

Run from the project root with its Python environment:
  python -m unittest -v tests.test_http_races
"""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
import hashlib
import importlib
import ipaddress
import json
import os
from pathlib import Path
import socket
import sys
import threading
import time
import unittest
from urllib.parse import urlsplit
from uuid import uuid4


ENVIRONMENT = ('ROADLENS_TEST_ORIGIN', 'ROADLENS_TEST_DB',
               'ROADLENS_TEST_DEVICE_TOKEN', 'ROADLENS_TEST_INTERNAL_TOKEN')


def loopback_origin(origin):
    parsed = urlsplit(origin)
    if (parsed.scheme not in ('http', 'https') or not parsed.hostname or
            parsed.username or parsed.password or parsed.query or parsed.fragment or
            parsed.path not in ('', '/')):
        raise ValueError('Test origin must be a plain loopback HTTP(S) origin')
    if parsed.hostname == 'localhost':
        addresses = {item[4][0] for item in socket.getaddrinfo('localhost', parsed.port or 80)}
        if not addresses or not all(ipaddress.ip_address(address).is_loopback for address in addresses):
            raise ValueError('localhost did not resolve exclusively to loopback')
    else:
        try:
            local = ipaddress.ip_address(parsed.hostname).is_loopback
        except ValueError:
            local = False
        if not local:
            raise ValueError('This test can target only a literal loopback address or localhost')
    return origin.rstrip('/')


def project_root():
    configured = os.environ.get('ROADLENS_TEST_PROJECT_ROOT')
    candidates = [Path(configured)] if configured else [Path.cwd(), Path.cwd() / 'roadlens',
        Path(__file__).resolve().parent.parent]
    for candidate in candidates:
        if (candidate / 'evals/workflow_contract.py').is_file() and (candidate / 'backend/models.py').is_file():
            return candidate.resolve()
    raise ValueError('Set ROADLENS_TEST_PROJECT_ROOT to the RoadLens checkout')


class LocalHTTPRaceTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        if not any(os.environ.get(name) for name in ENVIRONMENT):
            raise unittest.SkipTest('Opt-in local HTTP race fixture; ROADLENS_TEST_* variables are unset')
        if not all(os.environ.get(name) for name in ENVIRONMENT):
            raise ValueError('All four ROADLENS_TEST_* connection/database variables are required')
        if os.environ.get('ROADLENS_TEST_ISOLATED') != '1':
            raise ValueError('ROADLENS_TEST_ISOLATED=1 must acknowledge a disposable local preview with no real Modal worker')
        cls.origin = loopback_origin(os.environ['ROADLENS_TEST_ORIGIN'])
        root = project_root()
        sys.path.insert(0, str(root))
        cls.httpx = importlib.import_module('httpx')
        workflow = importlib.import_module('evals.workflow_contract')
        evidence = importlib.import_module('backend.evidence')
        cls.models = importlib.import_module('backend.models')
        cls.iso = staticmethod(workflow.iso)
        cls.probe = workflow.SQLiteProbe(os.environ['ROADLENS_TEST_DB'])
        cls.repo = evidence.Repository(root / 'data')
        cls.device_id = os.environ.get('ROADLENS_TEST_DEVICE_ID', 'aiy-box-1')
        cls.device_token = os.environ['ROADLENS_TEST_DEVICE_TOKEN']
        cls.internal_token = os.environ['ROADLENS_TEST_INTERNAL_TOKEN']
        cls.rounds = int(os.environ.get('ROADLENS_TEST_RACE_ROUNDS', '3'))
        if not 1 <= cls.rounds <= 4:
            raise ValueError('Race rounds must be between 1 and 4 to stay below device rate limits')
        cls.records = []
        cls.failure_ids = set()
        # Never repurpose a populated DB. There is intentionally no DELETE/reset.
        for table in ('drafts', 'reports', 'jobs', 'receipts', 'events', 'rates'):
            if cls.probe.query(f'SELECT COUNT(*) AS n FROM {table}')[0]['n']:
                raise ValueError('Requires an empty disposable local DB; found existing ' + table)
        with cls.httpx.Client(base_url=cls.origin, timeout=15, follow_redirects=False, trust_env=False) as client:
            response = client.get('/api/health')
            if response.status_code != 200 or response.json().get('status') != 'ok':
                raise ValueError('The local Site health check did not succeed')
        # A random cancellation is safe even if the origin accidentally points to
        # another local DB. Its state change proves HTTP and SQLiteProbe coincide.
        marker = cls.probe.draft(cls.repo, cls.device_id)
        response = cls.post(f'/api/device/drafts/{marker}/cancel', {})
        state = cls.probe.query('SELECT status FROM drafts WHERE id=?', (marker,))[0]['status']
        if response[0] != 200 or state != 'cancelled':
            raise ValueError('The HTTP preview did not cancel the draft in the specified isolated DB; check DB mapping/device ID/token')

    @classmethod
    def tearDownClass(cls):
        output = os.environ.get('ROADLENS_TEST_RESULT_PATH')
        if output and hasattr(cls, 'records'):
            path = Path(output)
            path.parent.mkdir(parents=True, exist_ok=True)
            result = {
                'scope': 'Disposable loopback HTTP infrastructure race fixtures; no AI evaluation, production or physical hardware claim',
                'analysis_callback_fixture': 'Strict Pydantic AnalysisResult; model fields explicitly say no model was run; no evidence metrics/records',
                'cases': cls.records,
                'passed': sum(item['passed'] for item in cls.records),
                'total': len(cls.records),
                'expected_case_records': 3 * cls.rounds + 2,
                'unittest_failures_or_errors': sorted(cls.failure_ids),
                'suite_outcome': 'pass' if not cls.failure_ids and len(cls.records) == 3 * cls.rounds + 2 and all(item['passed'] for item in cls.records) else 'fail',
                'isolated_database_retained': True,
            }
            fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
            os.fchmod(fd, 0o600)
            with os.fdopen(fd, 'w') as stream:
                json.dump(result, stream, indent=2)

    def tearDown(self):
        result = self._outcome.result
        prefix = self.__class__.__module__ + '.' + self.__class__.__qualname__ + '.'
        for test, _ in result.failures + result.errors:
            if test.id().startswith(prefix):
                self.failure_ids.add(test.id())

    @classmethod
    def post(cls, path, body, *, key=None, internal=False, barrier=None):
        headers = {'Content-Type': 'application/json', 'Authorization': 'Bearer ' +
                   (cls.internal_token if internal else cls.device_token)}
        if key:
            headers['Idempotency-Key'] = key
        try:
            with cls.httpx.Client(base_url=cls.origin, timeout=20, follow_redirects=False, trust_env=False) as client:
                if barrier:
                    barrier.wait(timeout=10)
                started = time.monotonic()
                response = client.post(path, json=body, headers=headers)
                elapsed = round((time.monotonic() - started) * 1000, 2)
                try:
                    payload = response.json()
                except ValueError:
                    payload = {'non_json_response': True}
                return response.status_code, payload, elapsed
        except Exception as error:
            raise AssertionError('Local request failed with ' + type(error).__name__ + '; credentials/request headers are omitted') from None

    def race(self, path, first, second, *, keys=(None, None), internal=False):
        barrier = threading.Barrier(2)
        with ThreadPoolExecutor(max_workers=2) as pool:
            tasks = [pool.submit(self.post, path, body, key=key, internal=internal, barrier=barrier)
                     for body, key in zip((first, second), keys)]
            return [task.result(timeout=35) for task in tasks]

    def record(self, case_id, checks, **evidence):
        row = {'case_id': case_id, 'checks': checks, 'passed': all(checks.values()), 'evidence': evidence}
        self.records.append(row)
        self.assertTrue(row['passed'], json.dumps({'case_id': case_id, 'failed_checks':
            [name for name, passed in checks.items() if not passed], 'evidence': evidence}, sort_keys=True))

    def draft_and_body(self):
        draft = self.probe.draft(self.repo, self.device_id)
        body = {'draft_id': draft, 'confirmation': {
            'resident_turn_id': 'race-confirm-' + uuid4().hex,
            'text': 'Yes, please submit it.', 'confirmed_at': self.iso()}}
        # Validate the same generated Python contract used by production.
        self.models.Submission.model_validate(body)
        return draft, body

    def related_counts(self, draft):
        return {
            'reports': self.probe.query('SELECT COUNT(*) n FROM reports WHERE draft_id=?', (draft,))[0]['n'],
            'jobs': self.probe.query('SELECT COUNT(*) n FROM jobs WHERE report_id IN(SELECT id FROM reports WHERE draft_id=?)', (draft,))[0]['n'],
            'receipts': self.probe.query('SELECT COUNT(*) n FROM receipts WHERE report_id IN(SELECT id FROM reports WHERE draft_id=?)', (draft,))[0]['n'],
            'received_events': self.probe.query("SELECT COUNT(*) n FROM events WHERE type='received' AND report_id IN(SELECT id FROM reports WHERE draft_id=?)", (draft,))[0]['n'],
        }

    def test_01_concurrent_identical_key_and_body_commits_once(self):
        for iteration in range(self.rounds):
            with self.subTest(round=iteration + 1):
                draft, body = self.draft_and_body()
                key = 'race-identical:' + uuid4().hex
                replies = self.race('/api/device/reports', body, body, keys=(key, key))
                statuses = sorted(reply[0] for reply in replies)
                ids = [reply[1].get('report_id') for reply in replies]
                counts = self.related_counts(draft)
                self.record('same_key_same_body:' + str(iteration + 1), {
                    'one_created_one_replayed': statuses == [200, 201],
                    'same_real_report_id': bool(ids[0]) and ids[0] == ids[1],
                    'one_report_job_receipt_and_received_event': counts == dict.fromkeys(counts, 1),
                }, statuses=statuses, report_ids=ids, counts=counts,
                   elapsed_ms=[reply[2] for reply in replies])

    def test_02_concurrent_same_key_different_valid_confirmation_conflicts(self):
        for iteration in range(self.rounds):
            with self.subTest(round=iteration + 1):
                draft, first = self.draft_and_body()
                second = json.loads(json.dumps(first))
                second['confirmation']['text'] = 'Yes, submit that.'
                second['confirmation']['resident_turn_id'] = 'race-confirm-' + uuid4().hex
                self.models.Submission.model_validate(second)
                key = 'race-conflict:' + uuid4().hex
                replies = self.race('/api/device/reports', first, second, keys=(key, key))
                statuses = sorted(reply[0] for reply in replies)
                counts = self.related_counts(draft)
                winner = next((index for index, reply in enumerate(replies) if reply[0] == 201), None)
                replay = self.post('/api/device/reports', (first, second)[winner], key=key) if winner is not None else (0, {}, 0)
                self.record('same_key_changed_body:' + str(iteration + 1), {
                    'one_created_one_conflict': statuses == [201, 409],
                    'winner_receipt_replays': winner is not None and replay[0] == 200 and replay[1].get('report_id') == replies[winner][1].get('report_id'),
                    'one_report_job_receipt_and_received_event': counts == dict.fromkeys(counts, 1),
                }, statuses=statuses, counts=counts, winner=winner,
                   elapsed_ms=[reply[2] for reply in replies])

    def test_03_consumed_draft_cannot_be_reused_with_another_key(self):
        draft, body = self.draft_and_body()
        first = self.post('/api/device/reports', body, key='race-first:' + uuid4().hex)
        second = self.post('/api/device/reports', body, key='race-other-key:' + uuid4().hex)
        counts = self.related_counts(draft)
        self.record('consumed_draft_other_key', {
            'initial_created': first[0] == 201,
            'new_key_conflicts': second[0] == 409,
            'one_report_job_receipt_and_received_event': counts == dict.fromkeys(counts, 1),
        }, statuses=[first[0], second[0]], counts=counts)

    def analysis_fixture(self):
        """Schema-valid infrastructure payload; never represented as AI evidence."""
        marker = 'INTEGRATION_FIXTURE_NO_MODEL_EXECUTED'
        output = self.models.NeedsClarification(missing_fields=['physical_inspection'],
            question='Infrastructure fixture only: no model or inspection was run. What would require on-site verification?')
        evidence = self.models.EvidenceBundle(sources=[], metrics=[], records=[], nearby=[],
            unknowns=['no_causal_evidence', 'current_imagery_not_supplied', 'unverified_resident_observation'])
        run = self.models.RunSummary(run_id='race-fixture-' + uuid4().hex, model=marker,
            model_version=marker, classifier=None, tools=[], validation_repairs=[],
            usage={'requests': 0, 'input_tokens': 0, 'output_tokens': 0}, elapsed_seconds=0,
            outcome='infrastructure_fixture_only', data_hashes={},
            prompt_hash=hashlib.sha256(marker.encode()).hexdigest(), code_commit='fixture-not-a-model-run')
        return self.models.AnalysisResult(output=output, evidence=evidence, run=run)

    def test_04_concurrent_duplicate_active_lease_completion_persists_one_event(self):
        for iteration in range(self.rounds):
            with self.subTest(round=iteration + 1):
                draft, body = self.draft_and_body()
                submitted = self.post('/api/device/reports', body, key='race-callback:' + uuid4().hex)
                self.assertEqual(submitted[0], 201, 'Fixture report must be durably created before claiming')
                report_id = submitted[1]['report_id']
                jobs = self.probe.query('SELECT * FROM jobs WHERE report_id=?', (report_id,))
                self.assertEqual(len(jobs), 1)
                job_id = jobs[0]['id']
                # Make this owned fixture earliest without touching other cases.
                self.probe.write('UPDATE jobs SET created_at=0 WHERE id=?', (job_id,))
                claimed = self.post('/api/internal/jobs/claim', {}, internal=True)
                job = claimed[1].get('job')
                self.assertEqual(claimed[0], 200)
                self.assertTrue(job and job.get('id') == job_id, 'Keep real/background workers disabled for this isolated fixture')
                payload_model = self.models.JobResult(version=1, lease_token=job['lease_token'],
                    result=self.analysis_fixture(), error=None)
                payload = payload_model.model_dump(mode='json')
                before = self.probe.query("SELECT COUNT(*) n FROM events WHERE report_id=? AND type='analysis_complete'", (report_id,))[0]['n']
                replies = self.race(f'/api/internal/jobs/{job_id}/result', payload, payload, internal=True)
                stored = self.probe.query('SELECT * FROM jobs WHERE id=?', (job_id,))[0]
                report = self.probe.query('SELECT * FROM reports WHERE id=?', (report_id,))[0]
                after = self.probe.query("SELECT COUNT(*) n FROM events WHERE report_id=? AND type='analysis_complete'", (report_id,))[0]['n']
                parsed = self.models.AnalysisResult.model_validate_json(stored['result']) if stored['result'] else None
                self.record('duplicate_active_lease_completion:' + str(iteration + 1), {
                    'both_callbacks_accepted': [reply[0] for reply in replies] == [200, 200],
                    'exactly_one_duplicate_ack': sum(reply[1].get('duplicate') is True for reply in replies) == 1,
                    'one_completion_event': after - before == 1,
                    'stored_result_exactly_matches_typed_fixture': parsed is not None and parsed.model_dump(mode='json') == payload['result'],
                    'job_completed_once': stored['state'] == 'complete' and stored['attempts'] == 1,
                    'analysis_complete_review_unchanged': report['analysis_status'] == 'complete' and report['review_status'] == 'awaiting_officer_review',
                    'one_report_job_receipt': all(value == 1 for value in self.related_counts(draft).values()),
                }, statuses=[reply[0] for reply in replies],
                   duplicate_flags=[reply[1].get('duplicate', False) for reply in replies],
                   completion_event_delta=after-before, job_id=job_id, report_id=report_id,
                   fixture='Strictly typed infrastructure fixture: no model, evidence query or physical inspection executed')

    def test_05_more_than_100_events_never_skip_a_page_or_report(self):
        baseline = self.probe.query('SELECT COALESCE(MAX(cursor),0) n FROM events')[0]['n']
        now = int(time.time() * 1000)
        report_ids = []
        # 125 distinct reports + 105 later updates = 230 events. This exercises
        # both distinct-report pagination and repeated events crossing a boundary.
        for index in range(125):
            draft = self.probe.draft(self.repo, self.device_id)
            report_id = 'race-page-' + uuid4().hex
            report_ids.append(report_id)
            self.probe.write('INSERT INTO reports(id,draft_id,issue,place_id,received_at,updated_at) VALUES(?,?,?,?,?,?)',
                (report_id, draft, 'visibility_obstruction', 'vicarage-st-matthews', now + index, now + index))
            self.probe.write("UPDATE drafts SET status='consumed',consumed_report_id=? WHERE id=?", (report_id, draft))
            self.probe.write('INSERT INTO events(report_id,type,created_at) VALUES(?,?,?)',
                (report_id, 'infrastructure_pagination_fixture', now + index))
        for index in range(105):
            self.probe.write('INSERT INTO events(report_id,type,created_at) VALUES(?,?,?)',
                (report_ids[index % len(report_ids)], 'infrastructure_pagination_fixture', now + 125 + index))
        expected = self.probe.query('SELECT cursor,report_id FROM events WHERE cursor>? ORDER BY cursor', (baseline,))
        self.assertEqual(len(expected), 230, 'Unexpected background events; isolate the local test database/worker')
        cursor, seen, pages, checks = baseline, set(), [], {}
        with self.httpx.Client(base_url=self.origin, timeout=15, follow_redirects=False, trust_env=False) as client:
            for page_index, start in enumerate(range(0, len(expected), 100), 1):
                response = client.get('/api/public/reports', params={'after': cursor})
                self.assertEqual(response.status_code, 200)
                page = response.json()
                expected_page = expected[start:start + 100]
                actual_ids = {item['report_id'] for item in page.get('reports', [])}
                expected_ids = {item['report_id'] for item in expected_page}
                checks[f'page_{page_index}_cursor_is_last_returned_event'] = page.get('cursor') == expected_page[-1]['cursor']
                checks[f'page_{page_index}_contains_exact_event_report_set'] = actual_ids == expected_ids
                checks[f'page_{page_index}_has_more_is_correct'] = page.get('has_more') == (len(expected_page) == 100)
                checks[f'page_{page_index}_is_not_cached'] = response.headers.get('cache-control') == 'no-store'
                seen.update(actual_ids)
                pages.append({'cursor': page.get('cursor'), 'distinct_reports': len(actual_ids),
                              'expected_event_count': len(expected_page), 'has_more': page.get('has_more')})
                cursor = page.get('cursor', cursor)
            final = client.get('/api/public/reports', params={'after': cursor})
            terminal = final.json()
        checks.update({'all_125_distinct_reports_seen': seen == set(report_ids),
            'all_230_event_positions_consumed': cursor == expected[-1]['cursor'],
            'terminal_page_empty_and_cursor_stable': final.status_code == 200 and terminal.get('reports') == [] and terminal.get('cursor') == cursor and terminal.get('has_more') is False})
        self.record('public_feed_over_100_events', checks, seeded_reports=125, seeded_events=230,
                    page_event_counts=[100, 100, 30], pages=pages)


if __name__ == '__main__':
    unittest.main(verbosity=2)
