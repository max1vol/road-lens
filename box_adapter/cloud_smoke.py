#!/usr/bin/env python3
"""Opt-in SYNTHETIC RoadLens integration rehearsal; never a physical box test.

Live mode uses the real Gemini WebSocket and cloud adapter with authored text
inputs. Returned PCM is counted/hashed and discarded: no microphone or speaker
is opened. API mode is the explicitly labelled CLI replay fallback and does not
claim to test Gemini, speech, playback or the adapter's acoustic readback gate.

Both modes create a real report in the configured Site when --submit is supplied.
The public metadata feed is then polled for the real analysis workflow status.
"""

import argparse
import asyncio
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import sys
from types import SimpleNamespace
from urllib.error import HTTPError, URLError
from urllib.request import Request
from uuid import uuid4

# Can live beside the voice files, or inside their tests directory.
HERE = Path(__file__).resolve().parent
if not (HERE / 'roadlens.py').exists():
    sys.path.insert(0, str(HERE.parent))

from roadlens import DeviceHTTP, RoadLens, canonical, utc_now


REPORT_TURNS = (
    'I cannot see past parked cars when I cross.',
    'Vicarage Terrace at St Matthews Street.',
)
CONFIRMATION = 'Yes, please submit it.'


class SmokeFailure(Exception):
    pass


def private_json(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    os.chmod(path.parent, 0o700)
    temp = path.with_suffix(path.suffix + '.tmp')
    fd = os.open(temp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    os.fchmod(fd, 0o600)
    with os.fdopen(fd, 'w') as stream:
        stream.write(canonical(value))
        stream.flush()
        os.fsync(stream.fileno())
    os.replace(temp, path)


def report_summary(report):
    return {key: report.get(key) for key in
            ('report_id', 'received_at', 'analysis_status', 'review_status')}


def get_feed(http, cursor):
    request = Request(http.base_url + '/api/public/reports?after=' + str(cursor),
                      headers={'Accept': 'application/json', 'Cache-Control': 'no-cache'})
    try:
        with http.opener.open(request, timeout=10) as response:
            if response.getcode() != 200:
                return None
            raw = response.read(512 * 1024 + 1)
        if len(raw) > 512 * 1024:
            return None
        result = json.loads(raw)
        if not isinstance(result, dict) or not isinstance(result.get('reports'), list):
            return None
        return result
    except (HTTPError, URLError, OSError, ValueError):
        return None


async def wait_for_analysis(http, receipt, seconds, run):
    report_id = receipt['report_id']
    deadline = asyncio.get_running_loop().time() + seconds
    cursor = 0
    transitions = []
    while asyncio.get_running_loop().time() < deadline:
        feed = await asyncio.to_thread(get_feed, http, cursor)
        if feed is not None:
            next_cursor = feed.get('cursor')
            if isinstance(next_cursor, int) and next_cursor >= cursor:
                cursor = next_cursor
            for report in feed['reports']:
                if isinstance(report, dict) and report.get('report_id') == report_id:
                    state = report.get('analysis_status')
                    if not transitions or transitions[-1]['analysis_status'] != state:
                        transitions.append({'observed_at': utc_now(), 'analysis_status': state})
                    run['public_feed_verified'] = True
                    run['public_report'] = report_summary(report)
                    run['analysis_transitions'] = transitions
                    if state == 'complete':
                        return
                    if state == 'failed':
                        raise SmokeFailure('The real analysis worker reported failed; report remains saved for review.')
        await asyncio.sleep(2)
    raise SmokeFailure('Timed out waiting for analysis completion in the real public feed.')


class MutedPCMSink:
    """Receives decoded model PCM without opening ALSA or representing playback."""
    def __init__(self):
        self.idle = asyncio.Event()
        self.idle.set()
        self.quiet_until = 0
        self.bytes_played = 0  # VoiceBox-compatible name; these bytes are discarded.
        self.segments = 0
        self.hash = hashlib.sha256()

    def put(self, data):
        if data is None:
            self.segments += 1
            self.idle.set()
            return
        self.idle.clear()
        self.bytes_played += len(data)
        self.hash.update(data)

    async def interrupt(self):
        self.idle.set()


def completed_tool(box, name, previous_ids):
    matches = [response for call_id, response in box.tool_results.items()
               if call_id not in previous_ids and response.get('name') == name]
    return matches[-1]['response'] if matches else None


async def live_turn(box, ws, receiver, text, tool_name, *, seconds, require_readback=False):
    previous_ids = set(box.tool_results)
    audio_before = box.speaker.bytes_played
    box.roadlens.transcript(text)
    await box.send(ws, {'clientContent': {'turns': [
        {'role': 'user', 'parts': [{'text': text}]}], 'turnComplete': True}})
    deadline = asyncio.get_running_loop().time() + seconds
    result = None
    while asyncio.get_running_loop().time() < deadline:
        if receiver.done():
            receiver.result()
            raise SmokeFailure('Gemini receiver ended before the requested tool completed.')
        result = completed_tool(box, tool_name, previous_ids)
        if result and 'error' in result:
            raise SmokeFailure(tool_name + ' returned an error; no success is assumed.')
        settled = (result is not None and not box.model_busy and not box.tool_tasks
                   and box.speaker.idle.is_set() and box.speaker.bytes_played > audio_before)
        if settled:
            if not require_readback:
                return result
            # This is deliberately a synthetic gate: real output transcription
            # and PCM arrived, but this muted test has no physical playback.
            if box.roadlens.mark_readback_complete():
                return result
            if box.roadlens._readback_after is not None:
                return result
        await asyncio.sleep(0.05)
    if require_readback and result:
        raise SmokeFailure('The complete exact readback was not verified in Gemini output transcription/PCM.')
    raise SmokeFailure(tool_name + ' did not finish with model IDLE and complete returned audio.')


async def live_smoke(args, http, directory, run):
    from aiy_gemini import VoiceBox, ENDPOINT, connect

    key = os.environ.get('GEMINI_API_KEY', '').strip()
    if not key:
        key = args.key_file.read_text().strip()
    if not key:
        raise SmokeFailure('The private Gemini key file is empty.')
    voice_args = SimpleNamespace(model=args.model, thinking_level=args.thinking_level,
                                 output_device='unused', roadlens=False)
    box = VoiceBox(voice_args, key)
    box.roadlens = RoadLens(directory=directory / 'adapter', transport=http)
    box.speaker = MutedPCMSink()
    box.roadlens.start()
    retry_task = asyncio.create_task(box.roadlens.retry_worker())
    receiver = None
    try:
        async with connect(ENDPOINT, additional_headers={'x-goog-api-key': key},
                           open_timeout=20, close_timeout=2, ping_interval=20,
                           ping_timeout=20, max_size=4 * 1024 * 1024) as ws:
            await box.open_session(ws)
            receiver = asyncio.create_task(box.receive(ws))
            run['stage'] = 'live_initial_report'
            first = await live_turn(box, ws, receiver, REPORT_TURNS[0], 'prepare_road_report',
                                    seconds=args.turn_timeout)
            if first.get('output', {}).get('kind') != 'needs_clarification':
                raise SmokeFailure('The location-free report did not request clarification.')
            run['location_clarification_verified'] = True
            run['stage'] = 'live_location_readback'
            draft = await live_turn(box, ws, receiver, REPORT_TURNS[1], 'prepare_road_report',
                                   seconds=args.turn_timeout, require_readback=True)
            if draft.get('output', {}).get('kind') != 'ready_for_confirmation':
                raise SmokeFailure('The exact fixture junction did not produce a stored draft.')
            run['draft_id'] = draft['draft_id']
            run['exact_model_readback_transcription_verified'] = True
            run['physical_readback_playback_verified'] = False
            run['stage'] = 'live_confirmation'
            await asyncio.sleep(0.01)
            response = await live_turn(box, ws, receiver, CONFIRMATION, 'submit_road_report',
                                      seconds=args.turn_timeout)
            run['live_submit_tool_called'] = True
            run['live_tool_returned_saved_receipt'] = bool(response.get('saved_to_roadlens'))
            if not response.get('saved_to_roadlens'):
                deadline = asyncio.get_running_loop().time() + args.turn_timeout
                while asyncio.get_running_loop().time() < deadline:
                    stored = box.roadlens._submission(draft['draft_id'])
                    if stored and stored['state'] == 'saved':
                        response = json.loads(stored['receipt'])
                        break
                    if stored and stored['state'] == 'blocked':
                        raise SmokeFailure('The real Site rejected the confirmed submission.')
                    await asyncio.sleep(0.2)
            if not response.get('saved_to_roadlens'):
                raise SmokeFailure('No durable server receipt received; private outbox remains available.')
            run['receipt'] = report_summary(response)
            run['tool_names_completed'] = [item['name'] for item in box.tool_results.values()]
            return response
    finally:
        run['pcm_bytes_received_and_discarded'] = box.speaker.bytes_played
        run['pcm_sha256'] = box.speaker.hash.hexdigest()
        run['completed_audio_segments_received'] = box.speaker.segments
        tasks = [task for task in [receiver, retry_task, *box.tool_tasks.values()] if task]
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        box.roadlens.stop()


async def api_smoke(args, http, directory, run):
    """HTTP-only fallback; does not emulate or claim successful acoustic gating."""
    session_id = str(uuid4())
    turns = []
    draft = None
    for index, text in enumerate(REPORT_TURNS):
        turns.append({'turn_id': str(uuid4()), 'text': text, 'timestamp': utc_now(), 'role': 'resident'})
        status, response = await asyncio.to_thread(http.post, '/api/device/prepare',
                                                   {'session_id': session_id, 'turns': turns})
        if status != 200:
            raise SmokeFailure('The real prepare endpoint did not return success.')
        expected = 'needs_clarification' if index == 0 else 'ready_for_confirmation'
        if response.get('output', {}).get('kind') != expected:
            raise SmokeFailure('The real prepare endpoint returned an unexpected outcome at replay step ' + str(index + 1))
        if index == 0:
            run['location_clarification_verified'] = True
        else:
            draft = response
    if not draft or not draft.get('draft_id') or not draft.get('readback'):
        raise SmokeFailure('The real server did not return a stored draft/readback.')
    run['draft_id'] = draft['draft_id']
    run['readback_source'] = 'Authoritative server response; no audio rendering in API replay mode'
    # A distinct authored confirmation turn follows the ready response. Allow
    # normal subsecond device/server clock skew in this automated replay; the
    # physical adapter instead waits for the complete spoken readback and reply.
    await asyncio.sleep(1.0)
    envelope = {'idempotency_key': str(uuid4()), 'body': {'draft_id': draft['draft_id'],
        'confirmation': {'resident_turn_id': str(uuid4()), 'text': CONFIRMATION,
                         'confirmed_at': utc_now()}}, 'synthetic': True}
    # Preserve exact confirmed replay contents before the first network attempt.
    private_json(directory / 'api-replay-envelope.json', envelope)
    run['stage'] = 'api_confirmation'
    deadline = asyncio.get_running_loop().time() + args.turn_timeout
    while asyncio.get_running_loop().time() < deadline:
        status, response = await asyncio.to_thread(http.post, '/api/device/reports',
            envelope['body'], envelope['idempotency_key'])
        if status in (200, 201) and response.get('report_id') and response.get('received_at'):
            run['receipt'] = report_summary(response)
            private_json(directory / 'server-receipt.json', response)
            return response
        if status and status < 500 and status not in (408, 425, 429):
            raise SmokeFailure('The real Site rejected the confirmed replay; HTTP status ' + str(status))
        await asyncio.sleep(5)
    raise SmokeFailure('No server receipt received; exact replay envelope is preserved for retry.')


async def main(args):
    if not args.submit:
        print('SYNTHETIC REHEARSAL ONLY. Pass --submit to create a real demonstration report. '
              'No microphone or speaker is used; this cannot verify physical hardware.')
        return 0
    directory = args.state_dir / (datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ') + '-' + uuid4().hex[:8])
    directory.mkdir(parents=True, mode=0o700)
    os.chmod(directory, 0o700)
    run = {'synthetic': True, 'physical_rehearsal': False, 'microphone_opened': False,
           'speaker_opened': False, 'mode': args.mode, 'model': args.model if args.mode == 'live' else None,
           'started_at': utc_now(), 'outcome': 'running', 'stage': 'configuration',
           'public_feed_verified': False, 'artifact_directory': str(directory)}
    try:
        http = DeviceHTTP(os.environ.get('ROADLENS_API_BASE_URL', ''), os.environ.get('ROADLENS_DEVICE_TOKEN', ''))
        run['site_origin'] = http.base_url
        run['stage'] = args.mode + '_prepare'
        receipt = await (live_smoke(args, http, directory, run) if args.mode == 'live'
                         else api_smoke(args, http, directory, run))
        run['stage'] = 'awaiting_real_analysis'
        private_json(directory / 'run.json', run)
        print(json.dumps({'synthetic': True, 'physical_rehearsal': False,
                          'report_id': receipt['report_id'], 'stage': run['stage']}, sort_keys=True), flush=True)
        await wait_for_analysis(http, receipt, args.analysis_timeout, run)
        run['stage'] = 'complete'
        run['outcome'] = 'pass'
    except SmokeFailure as error:
        run['outcome'] = 'fail'
        run['failure'] = str(error)
    except asyncio.CancelledError:
        run['outcome'] = 'fail'
        run['failure'] = 'Rehearsal interrupted; inspect the retained outbox before retrying.'
    except Exception as error:
        # Only a class name is exposed: provider/transport exception strings may
        # include credentials or sensitive request details.
        run['outcome'] = 'fail'
        run['failure'] = 'Unexpected ' + type(error).__name__ + '; no success is assumed.'
    finally:
        run['finished_at'] = utc_now()
        private_json(directory / 'run.json', run)
        print(json.dumps(run, sort_keys=True), flush=True)
    return 0 if run['outcome'] == 'pass' else 1


def parse_args():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--submit', action='store_true', help='Create a REAL synthetic demonstration report in the configured Site.')
    parser.add_argument('--mode', choices=('live', 'api'), default='live')
    parser.add_argument('--model', default='gemini-3.8-live-extended-thinking')
    parser.add_argument('--thinking-level', choices=('LOW', 'MEDIUM', 'HIGH'), default='MEDIUM')
    parser.add_argument('--key-file', type=Path, default=Path.home() / '.config/aiy-gemini-live/api-key')
    parser.add_argument('--state-dir', type=Path, default=Path.home() / '.local/state/roadlens-smoke')
    parser.add_argument('--turn-timeout', type=float, default=180)
    parser.add_argument('--analysis-timeout', type=float, default=240)
    args = parser.parse_args()
    if args.turn_timeout <= 0 or args.analysis_timeout <= 0:
        parser.error('Timeouts must be positive.')
    return args


if __name__ == '__main__':
    raise SystemExit(asyncio.run(main(parse_args())))
