"""Five common workflow scenarios against an isolated local Site HTTP server.

Uses the actual production routes and its local D1 SQLite backing file. It seeds a
ready draft to isolate infrastructure from AI quality; no model output is fabricated
as successful evidence. B08 simulates worker failure through the genuine private
result endpoint. This is not a physical-box test or a real Modal outage rehearsal.
"""
from __future__ import annotations
import argparse
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import sqlite3
import time
from urllib.parse import urlsplit
from uuid import uuid4
import httpx
from backend.evidence import Repository
from backend.models import ReadyDraft, EvidenceSpan, Issue


def iso(ms=None):
    return datetime.fromtimestamp((ms or time.time()*1000)/1000, timezone.utc).isoformat()


class SQLiteProbe:
    def __init__(self, path):
        self.path = Path(path).resolve()
        if not self.path.is_file():
            raise ValueError('A migrated isolated local D1 SQLite file is required')

    def query(self, sql, params=()):
        with sqlite3.connect(self.path, timeout=10) as db:
            db.row_factory = sqlite3.Row
            return [dict(row) for row in db.execute(sql, params).fetchall()]

    def write(self, sql, params=()):
        with sqlite3.connect(self.path, timeout=10) as db:
            db.execute(sql, params)

    def counts(self):
        return {table: self.query(f'SELECT COUNT(*) AS n FROM {table}')[0]['n'] for table in ('reports', 'jobs', 'receipts')}

    def draft(self, repo, device_id):
        draft_id = str(uuid4()); now = int(time.time()*1000) - 1000
        concern = 'I cannot see around parked cars at Vicarage Terrace and St Matthews Street.'
        output = ReadyDraft(issue=Issue.visibility_obstruction, observation=concern,
            location=repo.location('vicarage-st-matthews'), evidence_spans=[EvidenceSpan(turn_id='workflow-turn-1', quote=concern)],
            readback='A visibility concern at Vicarage Terrace / St Matthews Street. Shall I submit that to RoadLens for review?')
        turn = {'turn_id': 'workflow-turn-1', 'text': concern, 'role': 'resident', 'timestamp': iso(now-1000)}
        self.write('INSERT INTO drafts(id,device_id,session_id,output,turns,run,created_at,expires_at,status) VALUES(?,?,?,?,?,?,?,?,?)',
            [draft_id, device_id, 'workflow:' + uuid4().hex, output.model_dump_json(), json.dumps([turn]), '{}', now, now+900000, 'ready'])
        return draft_id


class WorkflowContract:
    def __init__(self, origin, device_token, internal_token, probe, repo, device_id, model_counter_file=None):
        parsed = urlsplit(origin)
        if parsed.hostname not in ('localhost', '127.0.0.1', '::1'):
            raise ValueError('This destructive test fixture is restricted to an isolated loopback Site')
        self.client = httpx.Client(base_url=origin.rstrip('/'), timeout=15, follow_redirects=False)
        self.device_token, self.internal_token = device_token, internal_token
        self.probe, self.repo, self.device_id = probe, repo, device_id
        self.model_counter_file = Path(model_counter_file) if model_counter_file else None
        self.records = []

    def call(self, path, body=None, *, role='device', key=None):
        headers = {'Content-Type': 'application/json'}
        token = {'device': self.device_token, 'internal': self.internal_token, 'wrong': 'not-a-valid-test-token', 'none': None}[role]
        if token:
            headers['Authorization'] = 'Bearer ' + token
        if key:
            headers['Idempotency-Key'] = key
        return self.client.post(path, json={} if body is None else body, headers=headers)

    def record(self, case_id, checks, **evidence):
        self.records.append({'case_id': case_id, 'checks': checks, 'passed': all(checks.values()), 'evidence': evidence})

    def counter(self):
        return int(self.model_counter_file.read_text()) if self.model_counter_file else None

    def run(self):
        initial = self.probe.counts()
        if any(initial.values()):
            raise ValueError('Workflow test requires an empty isolated database; it never clears user data')
        # B01: a stored ready draft is cancelled; a later attempted submit remains impossible.
        cancelled = self.probe.draft(self.repo, self.device_id)
        cancel_response = self.call(f'/api/device/drafts/{cancelled}/cancel')
        declined = {'draft_id': cancelled, 'confirmation': {'resident_turn_id': 'workflow-decline', 'text': 'No, do not submit.', 'confirmed_at': iso()}}
        rejected = self.call('/api/device/reports', declined, key='workflow:' + uuid4().hex)
        state = self.probe.query('SELECT status FROM drafts WHERE id=?', [cancelled])[0]['status']
        self.record('B01', {'cancel_acknowledged': cancel_response.status_code == 200, 'draft_cancelled': state == 'cancelled',
            'decline_rejected': rejected.status_code in (400, 409), 'no_report_or_job': self.probe.counts() == initial})
        # B02: deliberately discard the first successful HTTP response, then retry exact bytes/key.
        draft = self.probe.draft(self.repo, self.device_id)
        body = {'draft_id': draft, 'confirmation': {'resident_turn_id': 'workflow-confirm-' + uuid4().hex, 'text': 'Yes, please submit it.', 'confirmed_at': iso()}}
        key = 'workflow:' + uuid4().hex
        first = self.call('/api/device/reports', body, key=key)
        first_id = first.json().get('report_id')
        retry = self.call('/api/device/reports', body, key=key)
        self.probe.write('UPDATE drafts SET expires_at=? WHERE id=?', [int(time.time()*1000)-1, draft])
        expired_retry = self.call('/api/device/reports', body, key=key)
        different_key = self.call('/api/device/reports', body, key='workflow:' + uuid4().hex)
        self.record('B02', {'initial_commit_201': first.status_code == 201, 'retry_200': retry.status_code == 200,
            'same_report_id': bool(first_id) and retry.json().get('report_id') == first_id,
            'retry_after_expiry_returns_receipt': expired_retry.status_code == 200 and expired_retry.json().get('report_id') == first_id,
            'consumed_draft_new_key_rejected': different_key.status_code == 409,
            'one_report_job_receipt': self.probe.counts() == {'reports': 1, 'jobs': 1, 'receipts': 1}}, report_id=first_id)
        # B03: same idempotency identity cannot be repurposed for changed confirmation text.
        changed = json.loads(json.dumps(body)); changed['confirmation']['text'] = 'Yes please submit this changed content.'
        conflict = self.call('/api/device/reports', changed, key=key)
        self.record('B03', {'conflict_409': conflict.status_code == 409,
            'no_second_write': self.probe.counts() == {'reports': 1, 'jobs': 1, 'receipts': 1},
            'original_receipt_unchanged': self.call('/api/device/reports', body, key=key).json().get('report_id') == first_id})
        # B04: role checks happen before parsing/model work; no redirects count as denial.
        before = self.probe.counts(); calls_before = self.counter()
        bad = []
        for role in ('none', 'wrong'):
            for route in ('/api/device/prepare', '/api/device/reports'):
                bad.append(self.call(route, {'invalid': 'body'}, role=role))
        for route in ('/api/officer/ask', '/api/internal/jobs/claim', '/api/internal/jobs/not-a-job/result'):
            bad.append(self.call(route, {'invalid': 'body'}, role='device'))
        calls_after = self.counter()
        self.record('B04', {'all_denied_401_or_403': all(r.status_code in (401, 403) for r in bad),
            'no_writes': self.probe.counts() == before,
            'no_model_invocation': calls_before is not None and calls_before == calls_after},
            statuses=[r.status_code for r in bad], model_counter_measured=calls_before is not None)
        # B08: real claim/result contract, including duplicate claims and expired-lease rejection.
        claimed = self.call('/api/internal/jobs/claim', role='internal').json().get('job')
        if not claimed:
            self.record('B08', {'job_claimed': False}, reason='No claimable job; keep real workers disabled for isolated test')
            return self.records
        duplicate = self.call('/api/internal/jobs/claim', role='internal').json().get('job')
        self.probe.write('UPDATE jobs SET lease_expires=? WHERE id=?', [int(time.time()*1000)-1, claimed['id']])
        recovered = self.call('/api/internal/jobs/claim', role='internal').json().get('job')
        if not recovered:
            self.record('B08', {'expired_lease_recovered': False}); return self.records
        failure = lambda token: {'version': 1, 'lease_token': token, 'result': None, 'error': 'analysis_unavailable'}
        stale = self.call(f'/api/internal/jobs/{claimed["id"]}/result', failure(claimed['lease_token']), role='internal')
        retry_state = self.call(f'/api/internal/jobs/{recovered["id"]}/result', failure(recovered['lease_token']), role='internal')
        retried = self.call('/api/internal/jobs/claim', role='internal').json().get('job')
        final = self.call(f'/api/internal/jobs/{retried["id"]}/result', failure(retried['lease_token']), role='internal') if retried else None
        public = self.client.get('/api/public/reports').json().get('reports', [])
        visible = next((r for r in public if r['report_id'] == first_id), {})
        job = self.probe.query('SELECT * FROM jobs WHERE id=?', [claimed['id']])[0]
        self.record('B08', {'duplicate_worker_cannot_claim': duplicate is None,
            'same_job_recovered': recovered['id'] == claimed['id'], 'new_lease_token': recovered['lease_token'] != claimed['lease_token'],
            'expired_lease_result_rejected': stale.status_code == 409,
            'transient_failure_queued': retry_state.status_code == 200 and retry_state.json().get('state') == 'queued',
            'three_attempts_bounded': job['attempts'] == 3 and final is not None and final.json().get('state') == 'failed',
            'durable_received_report_visible': visible.get('analysis_status') == 'failed',
            'officer_review_unchanged': visible.get('review_status') == 'awaiting_officer_review',
            'analysis_not_fabricated': job['result'] is None,
            'no_duplicates': self.probe.counts() == {'reports': 1, 'jobs': 1, 'receipts': 1}},
            report_id=first_id, job_id=claimed['id'], method='Failure callback and expiry simulation on real Site routes; not an actual Modal outage')
        return self.records


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--origin', required=True)
    parser.add_argument('--sqlite', required=True)
    parser.add_argument('--device-token-file', required=True)
    parser.add_argument('--internal-token-file', required=True)
    parser.add_argument('--device-id', default='voice-box-1')
    parser.add_argument('--project-root', default=str(Path(__file__).resolve().parents[1]))
    parser.add_argument('--model-counter-file')
    parser.add_argument('--output', required=True)
    parser.add_argument('--isolated', action='store_true', help='Required: acknowledges an empty disposable local Site database')
    args = parser.parse_args()
    if not args.isolated:
        parser.error('--isolated is required; never target real report storage')
    contract = WorkflowContract(args.origin, Path(args.device_token_file).read_text().strip(), Path(args.internal_token_file).read_text().strip(),
        SQLiteProbe(args.sqlite), Repository(Path(args.project_root) / 'data'), args.device_id, args.model_counter_file)
    try:
        rows = contract.run()
        output = Path(args.output); output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(json.dumps({'scope': 'Five common infrastructure fixtures; no comparative AI score, physical hardware claim or real Modal outage claim', 'cases': rows, 'passed': sum(r['passed'] for r in rows), 'total': len(rows)}, indent=2))
        for row in rows:
            print(row['case_id'] + ': ' + ('pass' if row['passed'] else 'FAIL / UNMEASURED'))
    finally:
        contract.client.close()

if __name__ == '__main__':
    main()
