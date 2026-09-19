import asyncio
import struct
import unittest
from unittest.mock import patch

from aiy_gemini import Speaker, attenuate_pcm, parse_args


class SpeakerVolumeTests(unittest.TestCase):
    def test_lower_volume_preserves_duration_sign_and_silence(self):
        pcm = struct.pack('<5h', -32768, -1000, 0, 1000, 32767)
        quieter = attenuate_pcm(pcm, 0.5)
        self.assertEqual(len(quieter), len(pcm))
        self.assertEqual(struct.unpack('<5h', quieter), (-16384, -500, 0, 500, 16383))
        self.assertEqual(attenuate_pcm(pcm, 1.0), pcm)
        self.assertEqual(attenuate_pcm(pcm, 0.0), b'\0' * len(pcm))

    def test_private_service_setting_and_explicit_override(self):
        with patch.dict('os.environ', {'AIY_SPEAKER_VOLUME': '0.5'}):
            with patch('sys.argv', ['aiy_gemini.py']):
                self.assertEqual(parse_args().volume, 0.5)
            with patch('sys.argv', ['aiy_gemini.py', '--volume', '0.25']):
                self.assertEqual(parse_args().volume, 0.25)


class PlaybackVolumeTests(unittest.IsolatedAsyncioTestCase):
    async def test_actual_playback_write_is_attenuated(self):
        written = []
        class Process:
            returncode = None
            @property
            def stdin(self): return self
            def write(self, data): written.append(data)
            async def drain(self): pass
            def close(self): self.returncode = 0
            def terminate(self): self.returncode = -15
            async def wait(self): return self.returncode

        speaker = Speaker('unused', volume=0.5)
        with patch('aiy_gemini.asyncio.create_subprocess_exec', return_value=Process()):
            task = asyncio.create_task(speaker.run())
            try:
                speaker.put(struct.pack('<2h', 8000, -8000))
                speaker.put(None)
                await asyncio.wait_for(speaker.idle.wait(), 1)
                self.assertEqual(written, [struct.pack('<2h', 4000, -4000)])
                self.assertEqual(speaker.bytes_played, 4)
            finally:
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)


if __name__ == '__main__':
    unittest.main()
