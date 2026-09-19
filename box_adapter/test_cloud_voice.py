import asyncio
import tempfile
import unittest
from unittest.mock import patch

from roadlens import RoadLens
from test_cloud_adapter import Clock, Transport
from test_voice_box import FakeSocket, audio, box


class CloudVoiceTests(unittest.IsolatedAsyncioTestCase):
    async def test_pcm_and_output_transcript_reach_readback_gate(self):
        with tempfile.TemporaryDirectory() as directory:
            clock = Clock()
            voice = box()
            voice.roadlens = RoadLens(directory=directory, transport=Transport(clock), clock=clock)
            voice.roadlens.start()
            voice.roadlens.transcript('I cannot see past parked cars at Vicarage Terrace and St Matthews Street.')
            draft = await voice.roadlens.execute('prepare_road_report', {})
            message = {'serverContent': {'outputTranscription': {'text': draft['readback']},
                       'modelTurn': {'parts': [audio(b'\x00' * 320)]},
                       'turnComplete': True, 'interactionStatus': 'IDLE'}}
            await voice.receive(FakeSocket([message]), single_turn=True)
            self.assertEqual(voice.roadlens._readback_after, None)
            self.assertTrue(voice.roadlens._readback_audio)
            clock.advance()
            self.assertTrue(voice.roadlens.mark_readback_complete())
            self.assertIn('outputAudioTranscription', voice.setup()['setup'])

    async def test_microphone_gate_waits_for_idle_playback_echo_and_tools(self):
        with tempfile.TemporaryDirectory() as directory:
            clock = Clock()
            voice = box()
            voice.roadlens = RoadLens(directory=directory, transport=Transport(clock), clock=clock)
            voice.roadlens.start()
            voice.roadlens.transcript('Parked cars obstruct visibility at Vicarage Terrace and St Matthews Street.')
            draft = await voice.roadlens.execute('prepare_road_report', {})
            voice.roadlens.assistant_output(draft['readback'], audio=True)
            clock.advance()
            voice.speaker.idle.clear()
            socket = FakeSocket()

            class Process:
                returncode = None
                @property
                def stdout(self):
                    return self
                async def readexactly(self, count):
                    await asyncio.sleep(0.002)
                    return b'\0' * count
                def terminate(self):
                    self.returncode = -15
                async def wait(self):
                    return self.returncode

            with patch('aiy_gemini.asyncio.create_subprocess_exec', return_value=Process()):
                task = asyncio.create_task(voice.microphone(socket))
                try:
                    await asyncio.sleep(0.015)
                    self.assertIsNone(voice.roadlens._readback_after)
                    voice.speaker.idle.set()
                    voice.speaker.quiet_until = asyncio.get_running_loop().time() + 0.04
                    await asyncio.sleep(0.015)
                    self.assertIsNone(voice.roadlens._readback_after)
                    voice.model_busy = True
                    await asyncio.sleep(0.04)
                    self.assertIsNone(voice.roadlens._readback_after)
                    voice.model_busy = False
                    voice.tool_tasks['preparing'] = object()
                    await asyncio.sleep(0.015)
                    self.assertIsNone(voice.roadlens._readback_after)
                    voice.tool_tasks.clear()
                    await asyncio.sleep(0.015)
                    self.assertIsNotNone(voice.roadlens._readback_after)
                finally:
                    task.cancel()
                    await asyncio.gather(task, return_exceptions=True)


if __name__ == '__main__':
    unittest.main()
