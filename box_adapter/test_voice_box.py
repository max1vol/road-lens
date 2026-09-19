import asyncio
import base64
import json
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from aiy_gemini import VoiceBox


class FakeLED:
    lit = False

    def on(self):
        self.lit = True

    def off(self):
        self.lit = False

    def blink(self, **kwargs):
        self.lit = True


class FakeSpeaker:
    def __init__(self):
        self.items = []
        self.idle = asyncio.Event()
        self.idle.set()
        self.quiet_until = 0
        self.interrupted = False

    def put(self, data):
        self.items.append(data)

    async def interrupt(self):
        self.interrupted = True


class FakeSocket:
    def __init__(self, messages=()):
        self.messages = messages
        self.sent = []

    def __aiter__(self):
        return self.events()

    async def events(self):
        for message in self.messages:
            yield json.dumps(message)

    async def send(self, message):
        self.sent.append(json.loads(message))


def box(seconds=0.08):
    args = SimpleNamespace(
        model="gemini-3.8-live-extended-thinking", thinking_level="MEDIUM",
        input_device="fake", output_device="fake", idle_seconds=seconds,
        check=False, say=None,
    )
    instance = VoiceBox(args, "test-key")
    instance.led = FakeLED()
    instance.button = SimpleNamespace(is_pressed=True)
    instance.speaker = FakeSpeaker()
    return instance


def audio(value):
    return {"inlineData": {
        "mimeType": "audio/pcm;rate=24000",
        "data": base64.b64encode(value).decode(),
    }}


class VoiceBoxTests(unittest.IsolatedAsyncioTestCase):
    async def test_silence_timeout_cancels_session_and_switches_off_light(self):
        b = box()
        cancelled = asyncio.Event()

        async def connection():
            b.recording = True
            b.note_activity()
            try:
                await asyncio.Event().wait()
            finally:
                cancelled.set()

        b.connection = connection
        started = asyncio.get_running_loop().time()
        result = await b.conversation()
        elapsed = asyncio.get_running_loop().time() - started
        self.assertEqual(result, "silence timeout")
        self.assertGreaterEqual(elapsed, b.args.idle_seconds)
        self.assertLess(elapsed, 0.4)
        self.assertTrue(cancelled.is_set())
        self.assertFalse(b.led.lit)
        self.assertTrue(b.speaker.interrupted)

    async def test_speech_restarts_the_whole_listening_window(self):
        b = box(seconds=0.12)
        b.recording = True
        b.note_activity()
        waiting = asyncio.create_task(b.wait_for_silence())
        try:
            for _ in range(5):
                await asyncio.sleep(0.04)
                b.note_activity()
                self.assertFalse(waiting.done())
            last_spoken = asyncio.get_running_loop().time()
            await asyncio.wait_for(waiting, 0.4)
            self.assertGreaterEqual(asyncio.get_running_loop().time() - last_spoken, 0.11)
        finally:
            waiting.cancel()
            await asyncio.gather(waiting, return_exceptions=True)

    async def test_connecting_thinking_and_playback_do_not_use_listening_time(self):
        b = box(seconds=20)
        self.assertEqual(b.idle_remaining(0), 20)
        self.assertEqual(b.idle_remaining(90), 20)  # Connection delay.
        b.recording = True
        self.assertEqual(b.idle_remaining(100), 10)
        b.model_busy = True
        self.assertEqual(b.idle_remaining(180), 20)  # Long reasoning.
        b.model_busy = False
        b.speaker.idle.clear()
        self.assertEqual(b.idle_remaining(240), 20)  # Buffered audio still plays.
        b.speaker.idle.set()
        b.speaker.quiet_until = 240.3
        self.assertEqual(b.idle_remaining(240.2), 20)
        self.assertAlmostEqual(b.idle_remaining(250.2), 10)
        self.assertLessEqual(b.idle_remaining(260.3), 0)

    async def test_agent_tool_keeps_session_alive_until_work_finishes(self):
        b = box(seconds=20)
        b.recording = True
        b.last_activity = 0
        b.tool_tasks['pending-report'] = object()
        self.assertEqual(b.idle_remaining(90), 20)
        b.tool_tasks.clear()
        self.assertEqual(b.idle_remaining(100), 10)
        self.assertEqual(b.idle_remaining(110), 0)

    async def test_intermediate_turn_keeps_timer_paused_until_final_idle(self):
        b = box(seconds=20)
        b.recording = True
        stages = [
            ({"serverContent": {"interactionStatus": "IN_PROGRESS", "turnComplete": True}}, True),
            ({"serverContent": {"turnComplete": True}}, True),
            ({"interactionStatus": "IDLE"}, False),
        ]
        for message, busy in stages:
            with self.assertRaises(ConnectionError):
                await b.receive(FakeSocket([message]))
            self.assertEqual(b.model_busy, busy)

    async def test_transcription_resets_timer_without_logging_words(self):
        b = box(seconds=20)
        b.last_activity = 0
        for field in ("inputTranscription", "interimInputTranscription"):
            with self.assertRaises(ConnectionError):
                await b.receive(FakeSocket([{"serverContent": {field: {"text": "test speech"}}}]))
            self.assertGreater(b.last_activity, 0)
            b.last_activity = 0

    async def test_second_press_stops_before_timeout(self):
        b = box(seconds=2)

        async def connection():
            await asyncio.Event().wait()

        async def button_sequence():
            await asyncio.sleep(0.03)
            b.button.is_pressed = False
            await asyncio.sleep(0.05)
            b.button.is_pressed = True

        b.connection = connection
        sequence = asyncio.create_task(button_sequence())
        self.assertEqual(await asyncio.wait_for(b.conversation(), 0.5), "button pressed")
        await sequence
        self.assertFalse(b.led.lit)

    async def test_held_button_does_not_start_another_session(self):
        b = box()
        waiting = asyncio.create_task(b.wait_for_press())
        await asyncio.sleep(0.03)
        self.assertFalse(waiting.done())
        b.button.is_pressed = False
        await asyncio.sleep(0.04)
        self.assertFalse(waiting.done())
        b.button.is_pressed = True
        await asyncio.wait_for(waiting, 0.1)

    async def test_extended_thinking_continues_after_intermediate_turn(self):
        b = box()
        socket = FakeSocket([
            {"serverContent": {"modelTurn": {"parts": [audio(b"first")]},
                               "turnComplete": True, "interactionStatus": "IN_PROGRESS"}},
            {"serverContent": {"modelTurn": {"parts": [audio(b"second"), audio(b"third")]},
                               "turnComplete": True, "interactionStatus": "IDLE"}},
        ])
        await b.receive(socket, single_turn=True)
        self.assertEqual(b.speaker.items, [b"first", None, b"second", b"third", None])

    async def test_top_level_idle_status_ends_test_after_audio(self):
        b = box()
        socket = FakeSocket([
            {"serverContent": {"modelTurn": {"parts": [audio(b"reply")]}, "turnComplete": True}},
            {"interactionStatus": "IDLE"},
        ])
        await b.receive(socket, single_turn=True)
        self.assertIn(b"reply", b.speaker.items)

    async def test_microphone_suppresses_speaker_echo_and_closes_on_cancel(self):
        b = box()
        b.speaker.idle.clear()
        socket = FakeSocket()

        class Process:
            returncode = None

            @property
            def stdout(self):
                return self

            async def readexactly(self, count):
                await asyncio.sleep(0.002)
                return b"\0" * count

            def terminate(self):
                self.returncode = -15

            async def wait(self):
                return self.returncode

        process = Process()
        with patch("aiy_gemini.asyncio.create_subprocess_exec", return_value=process):
            recording = asyncio.create_task(b.microphone(socket))
            try:
                await asyncio.sleep(0.025)
                self.assertEqual(socket.sent, [])
                b.speaker.quiet_until = asyncio.get_running_loop().time() + 0.04
                b.speaker.idle.set()
                await asyncio.sleep(0.02)
                self.assertEqual(socket.sent, [])
                await asyncio.sleep(0.06)
                self.assertGreater(len(socket.sent), 0)
                # Continuous silent PCM is transmitted but does not count as speech.
                last_activity = b.last_activity
                await asyncio.sleep(0.03)
                self.assertEqual(b.last_activity, last_activity)
            finally:
                recording.cancel()
                await asyncio.gather(recording, return_exceptions=True)
        self.assertEqual(process.returncode, -15)
        self.assertFalse(b.recording)

    async def test_microphone_speech_keeps_session_alive_and_silence_expires(self):
        b = box(seconds=0.1)
        socket = FakeSocket()
        speaking = True

        class Process:
            returncode = None

            @property
            def stdout(self):
                return self

            async def readexactly(self, count):
                await asyncio.sleep(0.005)
                return b"\0" * count

            def terminate(self):
                self.returncode = -15

            async def wait(self):
                return self.returncode

        process = Process()
        with patch("aiy_gemini.asyncio.create_subprocess_exec", return_value=process), \
                patch("aiy_gemini.webrtcvad.Vad") as vad:
            vad.return_value.is_speech.side_effect = lambda *_: speaking
            recording = asyncio.create_task(b.microphone(socket))
            waiting = asyncio.create_task(b.wait_for_silence())
            try:
                await asyncio.sleep(0.25)
                self.assertFalse(waiting.done())
                self.assertGreater(vad.return_value.is_speech.call_count, 10)
                speaking = False
                await asyncio.wait_for(waiting, 0.3)
            finally:
                recording.cancel()
                waiting.cancel()
                await asyncio.gather(recording, waiting, return_exceptions=True)
        self.assertFalse(b.recording)


if __name__ == "__main__":
    unittest.main()
