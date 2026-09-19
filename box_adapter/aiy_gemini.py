#!/usr/bin/env python3
"""Gemini Live on the original Google AIY Voice HAT (ALSA + GPIO23/25)."""

import argparse
from array import array
import asyncio
import base64
import contextlib
import json
import logging
from logging.handlers import RotatingFileHandler
import math
import os
from pathlib import Path
import signal
import sys

from websockets.asyncio.client import connect
import webrtcvad

LOG = logging.getLogger("aiy-gemini")
ENDPOINT = (
    "wss://generativelanguage.googleapis.com/ws/"
    "google.ai.generativelanguage.v1beta.GenerativeService.BidiGenerateContent"
)
DEVICE = "plughw:CARD=sndrpigooglevoi,DEV=0"
INPUT_RATE = 16000
OUTPUT_RATE = 24000
CHUNK_BYTES = 640  # 20 ms, mono, signed 16-bit little-endian PCM.


def attenuate_pcm(data, volume):
    """Lower signed 16-bit PCM without changing its duration or sample format."""
    if volume == 1.0:
        return data
    samples = array('h')
    samples.frombytes(data)
    if sys.byteorder != 'little':
        samples.byteswap()
    for index, value in enumerate(samples):
        samples[index] = int(value * volume)
    if sys.byteorder != 'little':
        samples.byteswap()
    return samples.tobytes()


async def stop_process(process):
    if process is None or process.returncode is not None:
        return
    with contextlib.suppress(ProcessLookupError):
        process.terminate()
    try:
        await asyncio.wait_for(process.wait(), 2)
    except asyncio.TimeoutError:
        with contextlib.suppress(ProcessLookupError):
            process.kill()
        await process.wait()


class Speaker:
    """One playback owner; epoch changes invalidate buffered interrupted audio."""

    def __init__(self, device, volume=1.0):
        if not math.isfinite(volume) or not 0 <= volume <= 1:
            raise ValueError('Speaker volume must be between 0 and 1')
        self.device = device
        self.volume = volume
        self.queue = asyncio.Queue(maxsize=256)
        self.process = None
        self.epoch = 0
        self.idle = asyncio.Event()
        self.idle.set()
        self.bytes_played = 0
        self.quiet_until = 0.0

    def put(self, data):
        self.idle.clear()
        self.queue.put_nowait((self.epoch, data))

    async def interrupt(self):
        self.epoch += 1
        while not self.queue.empty():
            self.queue.get_nowait()
        await stop_process(self.process)
        self.idle.set()

    async def run(self):
        try:
            while True:
                epoch, data = await self.queue.get()
                if epoch != self.epoch:
                    continue
                if data is None:
                    if self.process and self.process.returncode is None:
                        self.process.stdin.close()
                        result = await self.process.wait()
                        if result and epoch == self.epoch:
                            raise RuntimeError(f"Speaker playback exited with status {result}")
                    self.process = None
                    if epoch == self.epoch:
                        self.quiet_until = asyncio.get_running_loop().time() + 0.3
                        self.idle.set()
                    continue
                if self.process is None or self.process.returncode is not None:
                    self.process = await asyncio.create_subprocess_exec(
                        "aplay", "-q", "-D", self.device, "-t", "raw", "-f", "S16_LE",
                        "-r", str(OUTPUT_RATE), "-c", "1", "--buffer-time=100000",
                        stdin=asyncio.subprocess.PIPE,
                    )
                if epoch != self.epoch:
                    await stop_process(self.process)
                    continue
                try:
                    self.process.stdin.write(attenuate_pcm(data, self.volume))
                    await self.process.stdin.drain()
                    self.bytes_played += len(data)
                except (BrokenPipeError, ConnectionResetError):
                    if epoch == self.epoch:
                        raise RuntimeError("AIY speaker stopped accepting audio") from None
        finally:
            await stop_process(self.process)


class VoiceBox:
    def __init__(self, args, key):
        self.args = args
        self.key = key
        self.speaker = Speaker(args.output_device, getattr(args, 'volume', 1.0))
        self.recording = False
        self.last_activity = None
        self.model_busy = False
        self.button = None
        self.led = None
        self.roadlens = None
        self.tool_tasks = {}
        self.tool_results = {}
        if getattr(args, 'roadlens', False):
            from roadlens import RoadLens
            self.roadlens = RoadLens(key)

    def setup(self):
        generation = {"responseModalities": ["AUDIO"]}
        if self.args.model.endswith("-extended-thinking"):
            generation["thinkingConfig"] = {"thinkingLevel": self.args.thinking_level}
        setup = {"setup": {
            "model": "models/" + self.args.model,
            "generationConfig": generation,
            "inputAudioTranscription": {},
            "systemInstruction": {"parts": [{"text": (
                "You are Gemini, a helpful voice assistant in a Raspberry Pi AIY Voice Box. "
                "A button press starts a short hands-free conversation. "
                "Respond to the person speaking to you. Keep spoken replies concise and natural "
                "unless asked for detail. Do not respond to unrelated background conversation. "
                "Do not claim to control devices or perform actions: no tools are connected."
            )}]},
            "realtimeInputConfig": {
                "automaticActivityDetection": {
                    "disabled": False,
                    "startOfSpeechSensitivity": "START_SENSITIVITY_LOW",
                    "endOfSpeechSensitivity": "END_SENSITIVITY_LOW",
                    "prefixPaddingMs": 200,
                    "silenceDurationMs": round(getattr(self.args, 'turn_silence_seconds', 8.0) * 1000),
                },
                "activityHandling": "START_OF_ACTIVITY_INTERRUPTS",
            },
        }}
        if self.roadlens:
            from roadlens import VOICE_INSTRUCTIONS, TOOL_DECLARATIONS
            setup['setup']['systemInstruction'] = {'parts': [{'text': VOICE_INSTRUCTIONS}]}
            setup['setup']['tools'] = [{'functionDeclarations': TOOL_DECLARATIONS}]
            setup['setup']['outputAudioTranscription'] = {}
        return setup

    async def send(self, ws, message):
        await ws.send(json.dumps(message))

    async def ask_opening_question(self, ws):
        # This is a device event, never a captured resident turn or confirmation.
        self.model_busy = True
        self.note_activity()
        await self.send(ws, {"clientContent": {
            "turns": [{"role": "user", "parts": [{"text":
                "[Device event: start button pressed. Ask the opening question now. "
                "This is not resident speech.]"}]}], "turnComplete": True,
        }})

    async def open_session(self, ws):
        await self.send(ws, self.setup())
        async with asyncio.timeout(20):
            while True:
                message = json.loads(await ws.recv())
                if "setupComplete" in message:
                    return
                if "error" in message:
                    raise RuntimeError(str(message["error"]))

    async def receive(self, ws, single_turn=False):
        received_audio = False
        async for raw in ws:
            message = json.loads(raw)
            if "error" in message:
                raise RuntimeError(str(message["error"]))
            if self.roadlens:
                for call_id in message.get('toolCallCancellation', {}).get('ids', []):
                    task = self.tool_tasks.get(call_id)
                    if task:
                        task.cancel()
                for call in message.get('toolCall', {}).get('functionCalls', []):
                    self.roadlens.assistant_started()
                    call_id = call.get('id')
                    if not call_id:
                        continue
                    if call_id in self.tool_results:
                        await self.send(ws, {'toolResponse': {'functionResponses': [self.tool_results[call_id]]}})
                    elif call_id not in self.tool_tasks:
                        self.tool_tasks[call_id] = asyncio.create_task(self.handle_tool(ws, call))
            content = message.get("serverContent", {})
            if self.roadlens:
                transcript = content.get('inputTranscription', {}).get('text', '')
                if transcript:
                    self.roadlens.transcript(transcript, finished=content.get('inputTranscription', {}).get('finished', False))
                spoken = content.get('outputTranscription', {}).get('text', '')
                if spoken:
                    self.roadlens.assistant_output(spoken)
            status = content.get("interactionStatus") or message.get("interactionStatus")
            was_busy = self.model_busy
            if content.get("interrupted"):
                self.model_busy = False
                await self.speaker.interrupt()
                if self.roadlens:
                    self.roadlens.assistant_interrupted()
                LOG.info("Previous reply interrupted")
            if content.get("modelTurn") or status == "IN_PROGRESS":
                self.model_busy = True
            if (status == "IDLE" or content.get("waitingForInput")
                    or (not self.args.model.endswith("-extended-thinking")
                        and content.get("turnComplete"))):
                self.model_busy = False
            if was_busy != self.model_busy or any(
                content.get(field, {}).get("text", "").strip()
                for field in ("inputTranscription", "interimInputTranscription")
            ):
                self.note_activity()
            for part in content.get("modelTurn", {}).get("parts", []):
                inline = part.get("inlineData", {})
                if inline.get("mimeType", "").startswith("audio/pcm"):
                    self.speaker.put(base64.b64decode(inline["data"], validate=True))
                    if self.roadlens:
                        self.roadlens.assistant_output(audio=True)
                    received_audio = True
            if content.get("turnComplete"):
                # A speech segment can finish while Extended Thinking continues.
                self.speaker.put(None)
                LOG.info("Speech segment received; reasoning state: %s", status or "unspecified")
            finished = status == "IDLE"
            if not self.args.model.endswith("-extended-thinking"):
                finished = finished or content.get("turnComplete", False)
            if single_turn and received_audio and finished:
                if not content.get("turnComplete"):
                    self.speaker.put(None)
                await self.speaker.idle.wait()
                return
        raise ConnectionError("Gemini closed the live connection")

    async def handle_tool(self, ws, call):
        call_id = call['id']
        name = call.get('name', '')
        self.note_activity()
        try:
            try:
                result = await self.roadlens.execute(name, call.get('args', {}))
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                # Neither the resident's words nor SDK exceptions containing credentials enter logs.
                LOG.warning('RoadLens action failed: %s', type(exc).__name__)
                self.roadlens.error()
                result = {'error': 'This action did not complete. Ask the resident to try again. Do not claim a report was saved.'}
            response = {'id': call_id, 'name': name, 'response': result}
            self.tool_results[call_id] = response
            await self.send(ws, {'toolResponse': {'functionResponses': [response]}})
            LOG.info('RoadLens tool completed: %s', name)
        except asyncio.CancelledError:
            LOG.info('RoadLens action cancelled: %s', name)
            raise
        except Exception as exc:
            LOG.warning('RoadLens tool response could not be delivered: %s', type(exc).__name__)
        finally:
            self.tool_tasks.pop(call_id, None)
            self.note_activity()

    async def microphone(self, ws):
        process = None
        sent_bytes = 0
        vad = webrtcvad.Vad(2)
        try:
            process = await asyncio.create_subprocess_exec(
                "arecord", "-q", "-D", self.args.input_device, "-t", "raw",
                "-f", "S16_LE", "-r", str(INPUT_RATE), "-c", "1",
                "--buffer-time=100000", "--period-time=20000",
                stdout=asyncio.subprocess.PIPE,
            )
            self.recording = True
            self.note_activity()
            self.led.on()
            if self.roadlens:
                self.roadlens.status('listening', 'Describe the road hazard. RoadLens will ask for any missing details.')
            LOG.info("Conversation live: speak naturally; press the button again to stop")
            while True:
                try:
                    data = await asyncio.wait_for(process.stdout.readexactly(CHUNK_BYTES), 3)
                except asyncio.IncompleteReadError:
                    raise RuntimeError("AIY microphone returned incomplete audio") from None
                # The original HAT has no echo canceller. Drain/discard capture
                # during playback and its acoustic tail so Gemini cannot hear itself.
                if (not self.speaker.idle.is_set()
                        or asyncio.get_running_loop().time() < self.speaker.quiet_until):
                    continue
                if self.roadlens and not self.model_busy and not self.tool_tasks:
                    # Consent starts only after the exact readback was spoken,
                    # playback and its acoustic tail drained, and Gemini is IDLE.
                    self.roadlens.mark_readback_complete()
                # Local speech detection keeps long utterances alive even before
                # Google sends a transcript. Silent PCM frames do not reset it.
                if vad.is_speech(data, INPUT_RATE):
                    self.note_activity()
                await self.send(ws, {"realtimeInput": {"audio": {
                    "data": base64.b64encode(data).decode("ascii"),
                    "mimeType": "audio/pcm;rate=16000",
                }}})
                sent_bytes += len(data)
        finally:
            await stop_process(process)
            self.recording = False
            LOG.info("Microphone stopped; streamed %.2f seconds", sent_bytes / (INPUT_RATE * 2))

    async def connection(self):
        # Keep the API key out of URLs, process arguments, and logs.
        async with connect(
            ENDPOINT, additional_headers={"x-goog-api-key": self.key},
            open_timeout=20, ping_interval=20, ping_timeout=20,
            close_timeout=2, max_size=4 * 1024 * 1024,
        ) as ws:
            await self.open_session(ws)
            LOG.info("Connected to %s", self.args.model)
            if self.args.check:
                return
            tasks = [asyncio.create_task(self.speaker.run())]
            try:
                if self.args.say:
                    await self.send(ws, {"clientContent": {
                        "turns": [{"role": "user", "parts": [{
                            "text": "Say this exactly, without adding anything: " + self.args.say,
                        }]}], "turnComplete": True,
                    }})
                    tasks.append(asyncio.create_task(self.receive(ws, single_turn=True)))
                else:
                    tasks.extend([
                        asyncio.create_task(self.receive(ws)),
                        asyncio.create_task(self.microphone(ws)),
                    ])
                    if self.roadlens:
                        await self.ask_opening_question(ws)
                done, _ = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
                for task in done:
                    task.result()
            finally:
                pending_tools = list(self.tool_tasks.values())
                for task in pending_tools:
                    task.cancel()
                await asyncio.gather(*pending_tools, return_exceptions=True)
                for task in tasks:
                    task.cancel()
                await asyncio.gather(*tasks, return_exceptions=True)
                await self.speaker.interrupt()

    async def wait_for_press(self):
        # Require a release followed by a new press; a held button never retriggers.
        while self.button.is_pressed:
            await asyncio.sleep(0.02)
        while not self.button.is_pressed:
            await asyncio.sleep(0.02)

    def note_activity(self):
        self.last_activity = asyncio.get_running_loop().time()

    def idle_remaining(self, now):
        # Give a full listening window after connection, reasoning, and playback.
        if (self.last_activity is None or not self.recording or self.model_busy or self.tool_tasks
                or not self.speaker.idle.is_set() or now < self.speaker.quiet_until):
            self.last_activity = now
        return self.args.idle_seconds - (now - self.last_activity)

    async def wait_for_silence(self):
        while True:
            remaining = self.idle_remaining(asyncio.get_running_loop().time())
            if remaining <= 0:
                return
            await asyncio.sleep(min(0.05, remaining))

    async def conversation(self):
        started = asyncio.get_running_loop().time()
        self.last_activity = None
        self.model_busy = False
        self.tool_results.clear()
        if self.roadlens:
            self.roadlens.start()
        self.led.blink(on_time=0.2, off_time=0.2, background=True)
        LOG.info("Conversation requested; silence timeout %.0f seconds", self.args.idle_seconds)
        tasks = [asyncio.create_task(self.connection()),
                 asyncio.create_task(self.wait_for_press()),
                 asyncio.create_task(self.wait_for_silence())]
        reason = "connection closed"
        try:
            done, _ = await asyncio.wait(
                tasks, return_when=asyncio.FIRST_COMPLETED,
            )
            if tasks[1] in done:
                reason = "button pressed"
            elif tasks[2] in done:
                reason = "silence timeout"
            for task in done:
                task.result()
            return reason
        finally:
            # A second button press can still stop a reply immediately.
            self.led.off()
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            await self.speaker.interrupt()
            if self.roadlens:
                self.roadlens.stop()
            LOG.info("Conversation ended (%s), elapsed %.2fs; microphone off",
                     reason, asyncio.get_running_loop().time() - started)

    async def run(self):
        if self.args.say or self.args.check:
            async with asyncio.timeout(60):
                await self.connection()
            if self.args.say:
                if not self.speaker.bytes_played:
                    raise RuntimeError("Gemini returned no playable audio")
                LOG.info("Speaker test completed: %.2f seconds of generated audio", self.speaker.bytes_played / 48000)
            return
        from gpiozero import Button, LED
        self.button = Button(self.args.button_pin, pull_up=True, bounce_time=0.04)
        self.led = LED(self.args.led_pin, initial_value=False)
        retry_task = asyncio.create_task(self.roadlens.retry_worker()) if self.roadlens else None
        try:
            while True:
                LOG.info("Ready: press once to talk; standby after %.0f seconds of silence",
                         self.args.idle_seconds)
                await self.wait_for_press()
                try:
                    await self.conversation()
                except Exception as exc:
                    detail = str(exc).replace(self.key, "[redacted]")
                    LOG.warning("Conversation stopped (%s): %s", type(exc).__name__, detail)
                # Never reconnect or restart the microphone without another press.
        finally:
            if retry_task:
                retry_task.cancel()
                await asyncio.gather(retry_task, return_exceptions=True)
            self.led.close()
            self.button.close()


def parse_args():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--model", default="gemini-3.8-live-extended-thinking")
    p.add_argument("--thinking-level", choices=["LOW", "MEDIUM", "HIGH"], default="MEDIUM")
    p.add_argument("--idle-seconds", type=float, default=20.0)
    p.add_argument("--turn-silence-seconds", type=float, default=8.0,
                   help="Wait this many seconds of silence before replying to speech")
    p.add_argument("--roadlens", action="store_true", help="Enable the RoadLens Pydantic AI reporting workflow")
    p.add_argument("--key-file", type=Path, default=Path.home() / ".config/aiy-gemini-live/api-key")
    p.add_argument("--input-device", default=DEVICE)
    p.add_argument("--output-device", default=DEVICE)
    p.add_argument("--volume", type=float, default=os.environ.get('AIY_SPEAKER_VOLUME', '1.0'),
                   help="Speaker output level from 0 (mute) to 1 (full); AIY_SPEAKER_VOLUME sets the default")
    p.add_argument("--button-pin", type=int, default=23)
    p.add_argument("--led-pin", type=int, default=25)
    modes = p.add_mutually_exclusive_group()
    modes.add_argument("--check", action="store_true", help="Verify a Live session without recording")
    modes.add_argument("--say", help="Speak a test phrase through the HAT without recording")
    args = p.parse_args()
    if not math.isfinite(args.idle_seconds) or args.idle_seconds <= 0:
        p.error("--idle-seconds must be a finite number greater than zero")
    if not math.isfinite(args.turn_silence_seconds) or args.turn_silence_seconds <= 0:
        p.error("--turn-silence-seconds must be a finite number greater than zero")
    if not math.isfinite(args.volume) or not 0 <= args.volume <= 1:
        p.error("--volume must be a finite number between 0 and 1")
    return args


async def main(args, key):
    task = asyncio.current_task()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, task.cancel)
    await VoiceBox(args, key).run()


if __name__ == "__main__":
    state_dir = Path.home() / ".local/state/aiy-gemini-live"
    state_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    log_file = state_dir / "service.log"
    fd = os.open(log_file, os.O_CREAT | os.O_WRONLY | os.O_APPEND, 0o600)
    os.close(fd)
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
        handlers=[logging.StreamHandler(), RotatingFileHandler(
            log_file, maxBytes=1_000_000, backupCount=2, encoding="utf-8",
        )],
    )
    args = parse_args()
    key = os.environ.get("GEMINI_API_KEY", "").strip()
    if not key:
        try:
            key = args.key_file.read_text().strip()
        except OSError:
            raise SystemExit("Missing Gemini key: configure the private key file or GEMINI_API_KEY")
    if not key:
        raise SystemExit("Gemini key is empty")
    try:
        asyncio.run(main(args, key))
    except (KeyboardInterrupt, asyncio.CancelledError):
        pass
    except Exception as exc:
        LOG.error("%s: %s", type(exc).__name__, str(exc).replace(key, "[redacted]"))
        raise SystemExit(1)
