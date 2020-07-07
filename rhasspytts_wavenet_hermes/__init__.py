"""Hermes MQTT server for Rhasspy TTS using Google Wavenet"""
import asyncio
import hashlib
import io
import logging
import os
import shlex
import subprocess
import typing
import wave
from uuid import uuid4

from google.cloud import texttospeech
from rhasspyhermes.audioserver import AudioPlayBytes, AudioPlayError, AudioPlayFinished
from rhasspyhermes.base import Message
from rhasspyhermes.client import GeneratorType, HermesClient, TopicArgs
from rhasspyhermes.tts import GetVoices, TtsError, TtsSay, TtsSayFinished, Voice, Voices

_LOGGER = logging.getLogger("rhasspytts_wavenet_hermes")

# -----------------------------------------------------------------------------


class TtsHermesMqtt(HermesClient):
    """Hermes MQTT server for Rhasspy TTS using Google Wavenet."""

    def __init__(
        self,
        client,
        wavenet_dir,
        voice,
        gender,
        sample_rate,
        language_code,
        play_command: typing.Optional[str] = None,
        site_ids: typing.Optional[typing.List[str]] = None,
    ):
        super().__init__("rhasspytts_wavenet_hermes", client, site_ids=site_ids)

        self.subscribe(TtsSay, GetVoices, AudioPlayFinished)

        self.wavenet_dir = wavenet_dir
        self.voice = voice
        self.gender = gender
        self.sample_rate = int(sample_rate)
        self.language_code = language_code
        self.play_command = play_command

        self.play_finished_events: typing.Dict[typing.Optional[str], asyncio.Event] = {}

        # Seconds added to playFinished timeout
        self.finished_timeout_extra: float = 0.25

        self.wavenet_client = None

        # Find credentials JSON file
        if os.path.isfile(os.path.join(self.wavenet_dir, "credentials.json")):

            _LOGGER.debug(
                "Trying credentials at %s",
                os.path.join(self.wavenet_dir, "credentials.json"),
            )

            # Set environment var
            os.environ["GOOGLE_APPLICATION_CREDENTIALS"] = os.path.join(
                self.wavenet_dir, "credentials.json"
            )

            self.wavenet_client = texttospeech.TextToSpeechClient()

        # Create cache directory in profile if it doesn't exist
        os.makedirs(os.path.join(self.wavenet_dir, "cache"), exist_ok=True)

    # -------------------------------------------------------------------------

    async def handle_say(
        self, say: TtsSay
    ) -> typing.AsyncIterable[
        typing.Union[
            TtsSayFinished,
            typing.Tuple[AudioPlayBytes, TopicArgs],
            TtsError,
            AudioPlayError,
        ]
    ]:
        """Run TTS system and publish WAV data."""
        wav_bytes: typing.Optional[bytes] = None

        try:
            # Try to pull WAV from cache first
            sentence_hash = self.get_sentence_hash(say.text)
            cached_wav_path = os.path.join(
                self.wavenet_dir, "cache", f"{sentence_hash.hexdigest()}.wav"
            )

            if os.path.isfile(cached_wav_path):
                # Use WAV file from cache
                _LOGGER.debug("Using WAV from cache: %s", cached_wav_path)
                with open(cached_wav_path, mode="rb") as cached_wav_file:
                    wav_bytes = cached_wav_file.read()

            if not wav_bytes:

                assert self.wavenet_client, "No Wavenet Client"

                _LOGGER.debug(
                    "Calling Wavenet (lang=%s, voice=%s, gender=%s, rate=%s)",
                    self.language_code,
                    self.voice,
                    self.gender,
                    self.sample_rate,
                )

                synthesis_input = texttospeech.SynthesisInput(text=say.text)

                voice_params = texttospeech.VoiceSelectionParams(
                    language_code=self.language_code,
                    name=self.language_code + "-" + self.voice,
                    ssml_gender=texttospeech.SsmlVoiceGender[self.gender],
                )

                audio_config = texttospeech.AudioConfig(
                    audio_encoding=texttospeech.AudioEncoding.LINEAR16,
                    sample_rate_hertz=self.sample_rate,
                )

                response = self.wavenet_client.synthesize_speech(
                    request={
                        "input": synthesis_input,
                        "voice": voice_params,
                        "audio_config": audio_config,
                    }
                )
                wav_bytes = response.audio_content

            assert wav_bytes, "No WAV data received"
            _LOGGER.debug("Got %s byte(s) of WAV data", len(wav_bytes))

            if wav_bytes:
                finished_event = asyncio.Event()

                # Play WAV
                if self.play_command:
                    try:
                        # Play locally
                        play_command = shlex.split(
                            self.play_command.format(lang=say.lang)
                        )
                        _LOGGER.debug(play_command)

                        subprocess.run(play_command, input=wav_bytes, check=True)

                        # Don't wait for playFinished
                        finished_event.set()
                    except Exception as e:
                        _LOGGER.exception("play_command")
                        yield AudioPlayError(
                            error=str(e),
                            context=say.id,
                            site_id=say.site_id,
                            session_id=say.session_id,
                        )
                else:
                    # Publish playBytes
                    request_id = say.id or str(uuid4())
                    self.play_finished_events[request_id] = finished_event

                    yield (
                        AudioPlayBytes(wav_bytes=wav_bytes),
                        {"site_id": say.site_id, "request_id": request_id},
                    )

                # Save to cache
                with open(cached_wav_path, "wb") as cached_wav_file:
                    cached_wav_file.write(wav_bytes)

                try:
                    # Wait for audio to finished playing or timeout
                    wav_duration = TtsHermesMqtt.get_wav_duration(wav_bytes)
                    wav_timeout = wav_duration + self.finished_timeout_extra

                    _LOGGER.debug("Waiting for play finished (timeout=%s)", wav_timeout)
                    await asyncio.wait_for(finished_event.wait(), timeout=wav_timeout)
                except asyncio.TimeoutError:
                    _LOGGER.warning("Did not receive playFinished before timeout")

        except Exception as e:
            _LOGGER.exception("handle_say")
            yield TtsError(
                error=str(e),
                context=say.id,
                site_id=say.site_id,
                session_id=say.session_id,
            )
        finally:
            yield TtsSayFinished(
                id=say.id, site_id=say.site_id, session_id=say.session_id
            )

    # -------------------------------------------------------------------------

    async def handle_get_voices(
        self, get_voices: GetVoices
    ) -> typing.AsyncIterable[Voices]:
        """Publish list of available voices. Currently does nothing."""
        voices: typing.List[Voice] = []

        # Publish response
        yield Voices(voices=voices, id=get_voices.id, site_id=get_voices.site_id)

    # -------------------------------------------------------------------------

    async def on_message(
        self,
        message: Message,
        site_id: typing.Optional[str] = None,
        session_id: typing.Optional[str] = None,
        topic: typing.Optional[str] = None,
    ) -> GeneratorType:
        """Received message from MQTT broker."""
        if isinstance(message, TtsSay):
            async for say_result in self.handle_say(message):
                yield say_result
        elif isinstance(message, GetVoices):
            async for voice_result in self.handle_get_voices(message):
                yield voice_result
        elif isinstance(message, AudioPlayFinished):
            # Signal audio play finished
            finished_event = self.play_finished_events.pop(message.id, None)
            if finished_event:
                finished_event.set()
        else:
            _LOGGER.warning("Unexpected message: %s", message)

    # -------------------------------------------------------------------------

    def get_sentence_hash(self, sentence: str):
        """Get hash for cache."""
        m = hashlib.md5()
        m.update(
            "_".join(
                [
                    sentence,
                    self.language_code + "-" + self.voice,
                    self.gender,
                    str(self.sample_rate),
                    self.language_code,
                ]
            ).encode("utf-8")
        )

        return m

    @staticmethod
    def get_wav_duration(wav_bytes: bytes) -> float:
        """Return the real-time duration of a WAV file"""
        with io.BytesIO(wav_bytes) as wav_buffer:
            wav_file: wave.Wave_read = wave.open(wav_buffer, "rb")
            with wav_file:
                width = wav_file.getsampwidth()
                rate = wav_file.getframerate()

                # getnframes is not reliable.
                # espeak inserts crazy large numbers.
                guess_frames = (len(wav_bytes) - 44) / width

                return guess_frames / float(rate)
