import asyncio
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import sqlite3
import stat
import tempfile
import unittest
from urllib.request import Request

from roadlens import DeviceHTTP, NoRedirect, RoadLens, TOOL_DECLARATIONS, is_affirmative


class Clock:
    def __init__(self):
        self.now = datetime(2026, 9, 19, 12, tzinfo=timezone.utc)

    def __call__(self):
        return self.now.isoformat()

    def advance(self, seconds=1):
        self.now += timedelta(seconds=seconds)


class Transport:
    def __init__(self, clock):
        self.clock = clock
        self.calls = []
        self.submission_results = []
        self.next_output = None
        self.counter = 0

    def post(self, path, body, key=None):
        self.calls.append((path, json.loads(json.dumps(body)), key))
        if path.endswith('/cancel'):
            return 204, {}
        if path.endswith('/prepare'):
            if self.next_output:
                result, self.next_output = self.next_output, None
                return result
            self.counter += 1
            readback = 'A visibility concern at Vicarage Terrace and St Matthews Street. Shall I submit that?'
            return 200, {
                'output': {'kind': 'ready_for_confirmation', 'issue': 'visibility_obstruction',
                           'observation': 'The resident reports difficulty seeing past parked cars.',
                           'readback': readback},
                'draft_id': 'draft-' + str(self.counter), 'readback': readback,
                'prepared_at': self.clock(),
                'expires_at': (self.clock.now + timedelta(minutes=15)).isoformat(),
            }
        if self.submission_results:
            return self.submission_results.pop(0)
        return 201, self.receipt()

    def receipt(self):
        return {'report_id': 'report-server-1', 'received_at': self.clock(),
                'analysis_status': 'queued', 'review_status': 'awaiting_officer_review'}

    @property
    def submissions(self):
        return [call for call in self.calls if call[0] == '/api/device/reports']


class AdapterTest(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.clock = Clock()
        self.transport = Transport(self.clock)
        self.box = RoadLens(directory=self.directory.name, transport=self.transport, clock=self.clock)
        self.box.start()

    def tearDown(self):
        self.directory.cleanup()

    async def prepare(self):
        self.box.transcript('I cannot see past parked cars when I cross.')
        self.clock.advance()
        self.box.transcript('Vicarage Terrace at St Matthews Street.')
        self.clock.advance()
        return await self.box.execute('prepare_road_report', {})

    def readback(self, draft):
        self.box.assistant_output(draft['readback'], audio=True)
        self.clock.advance()
        self.assertTrue(self.box.mark_readback_complete())

    def confirm(self, text='Yes, please submit it.'):
        self.clock.advance()
        self.box.transcript(text)

    async def test_actual_turns_only_and_tool_schema(self):
        refused = await self.box.execute('prepare_road_report', {'account': 'Invented car crash'})
        self.assertIn('error', refused)
        self.assertEqual(self.transport.calls, [])
        await self.prepare()
        body = self.transport.calls[0][1]
        self.assertEqual([turn['text'] for turn in body['turns']],
                         ['I cannot see past parked cars when I cross.', 'Vicarage Terrace at St Matthews Street.'])
        self.assertEqual(len({turn['turn_id'] for turn in body['turns']}), 2)
        self.assertTrue(all(turn['role'] == 'resident' for turn in body['turns']))
        self.assertEqual(TOOL_DECLARATIONS[0]['parameters']['properties'], {})
        self.assertEqual(set(TOOL_DECLARATIONS[1]['parameters']['properties']), {'draft_id'})

    async def test_happy_path_requires_server_receipt(self):
        draft = await self.prepare()
        self.readback(draft)
        self.confirm()
        result = await self.box.execute('submit_road_report', {'draft_id': draft['draft_id']})
        self.assertTrue(result['saved_to_roadlens'])
        self.assertFalse(result['sent_to_council'])
        self.assertEqual(result['report_id'], 'report-server-1')
        path, body, key = self.transport.submissions[0]
        self.assertEqual(body['confirmation']['text'], 'Yes, please submit it.')
        self.assertEqual(body['confirmation']['resident_turn_id'], self.box.transcripts[-1]['turn_id'])
        self.assertGreaterEqual(len(key), 32)

    async def test_consent_before_playback_is_rejected(self):
        draft = await self.prepare()
        self.confirm()
        rejected = await self.box.execute('submit_road_report', {'draft_id': draft['draft_id']})
        self.assertIn('error', rejected)
        self.readback(draft)
        rejected = await self.box.execute('submit_road_report', {'draft_id': draft['draft_id']})
        self.assertIn('error', rejected)
        self.assertEqual(self.transport.submissions, [])

    async def test_text_without_audio_cannot_complete_readback(self):
        draft = await self.prepare()
        self.box.assistant_output(draft['readback'])
        self.assertFalse(self.box.mark_readback_complete())

    async def test_audio_without_exact_readback_cannot_complete(self):
        await self.prepare()
        self.box.assistant_output('I have saved it already.', audio=True)
        self.assertFalse(self.box.mark_readback_complete())

    async def test_interrupted_readback_requires_complete_replay(self):
        draft = await self.prepare()
        self.box.assistant_output(draft['readback'], audio=True)
        self.box.assistant_interrupted()
        self.assertFalse(self.box.mark_readback_complete())
        self.box.assistant_output(draft['readback'], audio=True)
        self.clock.advance()
        self.assertTrue(self.box.mark_readback_complete())

    async def test_streamed_readback_and_confirmation(self):
        draft = await self.prepare()
        words = draft['readback'].split(' ')
        for index, word in enumerate(words):
            self.box.assistant_output((' ' if index else '') + word, audio=True)
        self.clock.advance()
        self.assertTrue(self.box.mark_readback_complete())
        self.clock.advance()
        self.box.transcript('Yes, ', finished=False)
        turn_id = self.box.transcripts[-1]['turn_id']
        self.box.transcript('please submit it.', finished=True)
        self.assertEqual(self.box.transcripts[-1]['turn_id'], turn_id)
        result = await self.box.execute('submit_road_report', {'draft_id': draft['draft_id']})
        self.assertTrue(result['saved_to_roadlens'])
        self.assertEqual(self.transport.submissions[0][1]['confirmation']['text'], 'Yes, please submit it.')

    async def test_negative_and_correction_invalidate_and_cancel_server_draft(self):
        draft = await self.prepare()
        self.readback(draft)
        self.confirm('No, actually it is a different junction.')
        result = await self.box.execute('submit_road_report', {'draft_id': draft['draft_id']})
        self.assertIn('error', result)
        self.assertIsNone(self.box.draft)
        await self.box.retry_pending(force=True)
        self.assertIn(('/api/device/drafts/draft-1/cancel', {}, None), self.transport.calls)
        self.assertEqual(self.transport.submissions, [])

    async def test_corrected_draft_requires_its_own_readback(self):
        draft = await self.prepare()
        self.readback(draft)
        self.confirm('Actually, New Street at East Road.')
        new = await self.box.execute('prepare_road_report', {})
        self.confirm()
        result = await self.box.execute('submit_road_report', {'draft_id': new['draft_id']})
        self.assertIn('error', result)
        self.assertNotEqual(new['draft_id'], draft['draft_id'])

    async def test_model_cannot_supply_fabricated_confirmation(self):
        draft = await self.prepare()
        self.readback(draft)
        result = await self.box.execute('submit_road_report', {
            'draft_id': draft['draft_id'], 'confirmed_by_resident': True,
            'confirmation': {'text': 'Yes'}})
        self.assertIn('error', result)
        self.assertEqual(self.transport.submissions, [])

    async def test_duplicate_submit_returns_same_receipt_without_second_write(self):
        draft = await self.prepare()
        self.readback(draft)
        self.confirm()
        first = await self.box.execute('submit_road_report', {'draft_id': draft['draft_id']})
        second = await self.box.execute('submit_road_report', {'draft_id': draft['draft_id']})
        self.assertEqual(first, second)
        self.assertEqual(len(self.transport.submissions), 1)

    async def test_network_unknown_persists_before_request_and_retries_exact_body(self):
        draft = await self.prepare()
        self.readback(draft)
        self.confirm()
        self.transport.submission_results = [(0, {})]
        original_post = self.transport.post
        def check_durable(path, body, key=None):
            if path == '/api/device/reports':
                with sqlite3.connect(self.box.database) as db:
                    row = db.execute('SELECT body,idempotency_key FROM submissions').fetchone()
                self.assertEqual(json.loads(row[0]), body)
                self.assertEqual(row[1], key)
            return original_post(path, body, key)
        self.transport.post = check_durable
        pending = await self.box.execute('submit_road_report', {'draft_id': draft['draft_id']})
        self.assertFalse(pending['saved_to_roadlens'])
        self.assertEqual(pending['status'], 'confirmation_pending')
        again = await self.box.execute('submit_road_report', {'draft_id': draft['draft_id']})
        self.assertTrue(again['saved_to_roadlens'])
        self.assertEqual(self.transport.submissions[0], self.transport.submissions[1])

    async def test_restart_retries_same_key_even_after_draft_expiry(self):
        draft = await self.prepare()
        self.readback(draft)
        self.confirm()
        self.transport.submission_results = [(0, {})]
        await self.box.execute('submit_road_report', {'draft_id': draft['draft_id']})
        original = self.transport.submissions[0]
        self.box.stop()
        self.clock.advance(3600)
        new_transport = Transport(self.clock)
        new_transport.submission_results = [(200, new_transport.receipt())]
        restarted = RoadLens(directory=self.directory.name, transport=new_transport, clock=self.clock)
        results = await restarted.retry_pending(force=True)
        self.assertTrue(results[0]['saved_to_roadlens'])
        self.assertEqual(new_transport.submissions[0], original)
        self.assertFalse(restarted.active)
        self.assertEqual([call[0] for call in new_transport.calls], ['/api/device/reports'])

    async def test_uncommitted_expired_draft_cannot_be_submitted(self):
        draft = await self.prepare()
        self.readback(draft)
        self.confirm()
        self.clock.advance(901)
        result = await self.box.execute('submit_road_report', {'draft_id': draft['draft_id']})
        self.assertIn('expired', result['error'])
        self.assertEqual(self.transport.submissions, [])

    async def test_server_expiry_does_not_silently_prepare_or_change_payload(self):
        draft = await self.prepare()
        self.readback(draft)
        self.confirm()
        self.transport.submission_results = [(410, {'error': 'expired'})]
        result = await self.box.execute('submit_road_report', {'draft_id': draft['draft_id']})
        self.assertEqual(result['status'], 'not_confirmed')
        await self.box.retry_pending(force=True)
        self.assertEqual(len(self.transport.submissions), 1)
        self.assertEqual(sum(call[0].endswith('/prepare') for call in self.transport.calls), 1)

    async def test_rejected_or_malformed_receipt_never_claims_saved(self):
        draft = await self.prepare()
        self.readback(draft)
        self.confirm()
        self.transport.submission_results = [(201, {'report_id': 'incomplete'})]
        result = await self.box.execute('submit_road_report', {'draft_id': draft['draft_id']})
        self.assertFalse(result['saved_to_roadlens'])

    async def test_private_storage_and_sanitized_local_status(self):
        await self.prepare()
        for path in [Path(self.directory.name), self.box.database, self.box.board.path]:
            self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o700 if path.is_dir() else 0o600)
        state = self.box.board.path.read_text()
        self.assertNotIn('I cannot see past parked cars when I cross.', state)
        self.assertNotIn('turn_id', state)
        self.assertNotIn('session_id', state)

    async def test_no_fallback_when_no_transcript(self):
        result = await self.box.execute('prepare_road_report', {})
        self.assertIn('error', result)
        self.assertEqual(self.transport.calls, [])

    async def test_oversize_transcript_rejected_without_silent_truncation(self):
        self.box.transcript('x' * 4001)
        result = await self.box.execute('prepare_road_report', {})
        self.assertIn('too long', result['error'])
        self.assertEqual(self.transport.calls, [])

    async def test_clarification_is_not_a_draft(self):
        self.transport.next_output = (200, {'output': {
            'kind': 'needs_clarification', 'missing_fields': ['location'],
            'question': 'Which junction?'}})
        result = await self.prepare()
        self.assertEqual(result['output']['kind'], 'needs_clarification')
        self.assertIsNone(self.box.draft)
        self.assertEqual(self.transport.submissions, [])

    async def test_background_worker_retries_in_standby(self):
        draft = await self.prepare()
        self.readback(draft)
        self.confirm()
        self.transport.submission_results = [(503, {})]
        await self.box.execute('submit_road_report', {'draft_id': draft['draft_id']})
        self.box.stop()
        self.clock.advance(10)
        task = asyncio.create_task(self.box.retry_worker())
        for _ in range(50):
            if len(self.transport.submissions) == 2:
                break
            await asyncio.sleep(0.01)
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        self.assertEqual(len(self.transport.submissions), 2)
        self.assertFalse(self.box.active)


class ConsentAndTransportTest(unittest.TestCase):
    def test_refusals_conditionals_quotes_and_guesses_are_not_confirmation(self):
        for text in ['No', "Don't submit it", 'Yes but change the road', 'Yes if you remove my name',
                     'She said yes', 'I might say yes later', 'Yes, not yet', 'Yes? No.',
                     'Go ahead if it is anonymous', 'Please do not submit it', 'Can you submit it?',
                     'I confirm that cars are parked there', 'Actually yes, change it first']:
            with self.subTest(text=text):
                self.assertFalse(is_affirmative(text))

    def test_complete_plain_affirmations(self):
        for text in ['Yes', 'Yes, please submit it.', 'Please save the report.', 'Go ahead',
                     'Okay, submit it', 'Yes please', 'Yes go ahead and submit it']:
            with self.subTest(text=text):
                self.assertTrue(is_affirmative(text))

    def test_http_redirects_are_never_followed(self):
        redirect = NoRedirect().redirect_request(Request('https://roadlens.example'), None,
            302, 'Found', {}, 'https://another.example')
        self.assertIsNone(redirect)

    def test_config_rejects_non_https_and_embedded_credentials(self):
        for url in ['http://example.com', 'https://token@example.com', 'https://example.com?token=secret',
                    'https://example.com/path', 'https://example.com#token', '']:
            with self.subTest(url=url):
                with self.assertRaises(ValueError):
                    DeviceHTTP(url, 'a' * 64)
        with self.assertRaises(ValueError):
            DeviceHTTP('https://example.com', 'short')


if __name__ == '__main__':
    unittest.main()
