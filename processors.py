import asyncio
from pipecat.processors.frame_processor import FrameProcessor, FrameDirection
from pipecat.frames.frames import (
    Frame,
    BotStartedSpeakingFrame,
    BotStoppedSpeakingFrame,
    TranscriptionFrame,
    LLMContextFrame,
    TextFrame,
    AudioRawFrame,
    MetricsFrame,
    TTSAudioRawFrame,
    TTSStoppedFrame,
    InterruptionFrame,
)
from loguru import logger

class SpeechTracker(FrameProcessor):
    """
    Custom Pipecat FrameProcessor for tracking user and bot speech events,
    VAD status, chronologically recording call history transcripts, and
    structuring turn tasks for real-time Slack updates.
    """
    def __init__(self, call_history: list = None, context = None, on_turn_update = None, call_logger = None):
        super().__init__()
        self.is_speaking = False
        self.first_utterance_finished = False
        self.call_history = call_history if call_history is not None else []
        self.context = context
        self.tasks = []  # List of turn dicts for Slack plan block schema
        self.on_turn_update = on_turn_update
        self.call_logger = call_logger
        self.current_task = None

    def add_task_detail(self, detail_text: str):
        """Adds internal tool/action detail to the active turn task."""
        if self.current_task:
            self.current_task["details_text"] = detail_text
        elif self.tasks:
            self.tasks[-1]["details_text"] = detail_text
        if self.on_turn_update:
            asyncio.create_task(self._trigger_update())

    def sync_from_context(self, context_messages: list):
        """
        Reconciles tasks list from LLM context messages to ensure turns
        are accurately captured even if standalone TranscriptionFrames are missing.
        """
        user_turns = []
        current_user_msg = None
        
        for msg in context_messages:
            role = msg.get("role")
            content = str(msg.get("content") or "").strip()
            
            # Skip system/developer messages and operator instructions
            if role == "developer" or content.startswith("SYSTEM INFO:") or content.startswith("INSTRUCTION:"):
                continue
                
            if role == "user":
                if current_user_msg:
                    user_turns.append(current_user_msg)
                current_user_msg = {
                    "title": content,
                    "output_text": "",
                    "details_text": "",
                    "status": "in_progress"
                }
            elif role == "assistant" and current_user_msg:
                current_user_msg["output_text"] = content
                current_user_msg["status"] = "complete"
                user_turns.append(current_user_msg)
                current_user_msg = None
                
        if current_user_msg:
            user_turns.append(current_user_msg)

        if not user_turns:
            return

        # Update tasks while preserving task_ids and details_text
        new_tasks = []
        for idx, turn in enumerate(user_turns, 1):
            task_id = f"task_{idx}"
            existing_detail = ""
            existing_status = ""
            if idx - 1 < len(self.tasks):
                existing_detail = self.tasks[idx - 1].get("details_text", "")
                existing_status = self.tasks[idx - 1].get("status", "")
                
            new_task = {
                "task_id": task_id,
                "title": turn["title"],
                "status": turn["status"],
                "output_text": turn["output_text"],
                "details_text": existing_detail or turn["details_text"]
            }
            new_tasks.append(new_task)
            
            # If a turn just became complete, log its output and add to the flat call_history
            if turn["status"] == "complete" and existing_status != "complete" and turn["output_text"]:
                if self.call_logger:
                    self.call_logger.info(f"[Transcription:bot] [{turn['output_text']}]")
                self.call_history.append(f"[Bot] {turn['output_text']}")
            
        self.tasks = new_tasks
        if self.tasks:
            self.current_task = self.tasks[-1]

    async def _trigger_update(self):
        if self.on_turn_update:
            try:
                res = self.on_turn_update(self.tasks)
                if asyncio.iscoroutine(res):
                    await res
            except Exception as e:
                pass

    async def process_frame(self, frame: Frame, direction: FrameDirection):
        await super().process_frame(frame, direction)
        
        # Continuously sync from context if context reference exists
        if self.context and hasattr(self.context, "messages"):
            old_task_count = len(self.tasks)
            self.sync_from_context(self.context.messages)
            if len(self.tasks) != old_task_count:
                await self._trigger_update()

        if isinstance(frame, BotStartedSpeakingFrame):
            self.is_speaking = True
            if self.call_logger:
                self.call_logger.info("Bot started speaking")
        elif isinstance(frame, BotStoppedSpeakingFrame):
            self.is_speaking = False
            self.first_utterance_finished = True
            if self.call_logger:
                self.call_logger.info("Bot stopped speaking")
            if self.current_task and self.current_task.get("status") == "in_progress":
                self.current_task["status"] = "complete"
                await self._trigger_update()
        elif isinstance(frame, TranscriptionFrame):
            text = (frame.text or "").strip()
            if text:
                if frame.user_id == "user":
                    self.call_history.append(f"[User] {text}")
                    if self.call_logger:
                        self.call_logger.info(f"[Transcription:user] [{text}]")
                    task_id = f"task_{len(self.tasks) + 1}"
                    self.current_task = {
                        "task_id": task_id,
                        "title": text,
                        "status": "in_progress",
                        "output_text": "",
                        "details_text": ""
                    }
                    self.tasks.append(self.current_task)
                    await self._trigger_update()
                else:
                    self.call_history.append(f"[Bot] {text}")
                    if self.call_logger:
                        self.call_logger.info(f"[Transcription:bot] [{text}]")
                    if self.current_task:
                        self.current_task["output_text"] = text
                        await self._trigger_update()
        elif isinstance(frame, TextFrame):
            text = (frame.text or "").strip()
            if text and self.current_task:
                self.current_task["output_text"] = text
                await self._trigger_update()
        elif isinstance(frame, LLMContextFrame):
            if frame.context and frame.context.messages:
                self.sync_from_context(frame.context.messages)
                await self._trigger_update()

        await self.push_frame(frame, direction)


class MetricsLogger(FrameProcessor):
    """
    Custom Pipecat FrameProcessor for extracting and logging token usage metrics natively.
    """
    def __init__(self, call_logger):
        super().__init__()
        self.call_logger = call_logger

    async def process_frame(self, frame: Frame, direction: FrameDirection):
        await super().process_frame(frame, direction)
        if isinstance(frame, MetricsFrame):
            if self.call_logger:
                # Stringify the frame data naturally
                self.call_logger.info(f"Metrics: {frame}")
        await self.push_frame(frame, direction)


class CallerMuter(FrameProcessor):
    """
    Custom Pipecat FrameProcessor for muting incoming caller audio
    until the bot completes its initial greeting utterance.
    """
    def __init__(self, tracker: SpeechTracker):
        super().__init__()
        self.tracker = tracker

    async def process_frame(self, frame: Frame, direction: FrameDirection):
        await super().process_frame(frame, direction)
        # Mute caller audio until the bot finishes its first utterance
        if not self.tracker.first_utterance_finished and isinstance(frame, AudioRawFrame) and direction == FrameDirection.DOWNSTREAM:
            return  # Drop caller audio
        await self.push_frame(frame, direction)


class JitterBufferProcessor(FrameProcessor):
    """Buffers the first N milliseconds of each bot utterance before releasing
    them to the transport, creating a head-start that absorbs Gemini's
    generation jitter.

    The FastAPI WebSocket transport drains its audio queue at 2x real-time
    (send_interval = chunk_duration / 2).  Without buffering, even a brief
    stutter in Gemini's audio generation starves the queue and produces an
    audible gap on the phone.  By holding back the initial burst and then
    flushing it all at once, the transport's pacing loop naturally builds a
    backlog that acts as a continuous jitter buffer for the rest of the
    utterance.

    Non-audio frames always pass through immediately.
    """

    # States
    _STATE_WAITING = "waiting"      # No audio yet, waiting for first frame
    _STATE_BUFFERING = "buffering"  # Accumulating initial audio
    _STATE_PASSTHROUGH = "passing"  # Buffer flushed, passing frames directly

    def __init__(self, buffer_ms: int = 200, sample_rate: int = 8000, call_logger=None):
        super().__init__()
        self._buffer_ms = buffer_ms
        self._sample_rate = sample_rate
        self._call_logger = call_logger
        # 16-bit mono PCM: 2 bytes per sample
        self._threshold_bytes = int(sample_rate * 2 * (buffer_ms / 1000))
        self._reset()

    def _reset(self):
        """Reset to the waiting state for a new utterance."""
        self._state = self._STATE_WAITING
        self._audio_buffer = bytearray()
        self._buffered_frames: list[TTSAudioRawFrame] = []

    async def _flush_buffer(self):
        """Release all buffered audio frames downstream."""
        if self._buffered_frames:
            if self._call_logger:
                self._call_logger.debug(
                    f"JitterBuffer: flushing {len(self._audio_buffer)} bytes "
                    f"({len(self._buffered_frames)} frames) after "
                    f"{len(self._audio_buffer) / (self._sample_rate * 2) * 1000:.0f}ms"
                )
            for frame in self._buffered_frames:
                await self.push_frame(frame, FrameDirection.DOWNSTREAM)
            self._audio_buffer = bytearray()
            self._buffered_frames = []

    async def process_frame(self, frame: Frame, direction: FrameDirection):
        await super().process_frame(frame, direction)

        # --- Reset triggers (upstream or downstream) ---
        if isinstance(frame, (TTSStoppedFrame, InterruptionFrame)):
            if self._state == self._STATE_BUFFERING:
                # Flush whatever we have so it isn't lost
                await self._flush_buffer()
            self._reset()
            await self.push_frame(frame, direction)
            return

        # --- Only buffer downstream TTS audio ---
        if isinstance(frame, TTSAudioRawFrame) and direction == FrameDirection.DOWNSTREAM:
            if self._state == self._STATE_WAITING:
                self._state = self._STATE_BUFFERING
                if self._call_logger:
                    self._call_logger.debug(
                        f"JitterBuffer: started buffering (threshold={self._threshold_bytes}B / {self._buffer_ms}ms)"
                    )

            if self._state == self._STATE_BUFFERING:
                self._audio_buffer.extend(frame.audio)
                self._buffered_frames.append(frame)

                if len(self._audio_buffer) >= self._threshold_bytes:
                    # Threshold met — flush everything and switch to passthrough
                    await self._flush_buffer()
                    self._state = self._STATE_PASSTHROUGH
                return  # Don't push yet while buffering

            # _STATE_PASSTHROUGH — let it through immediately
            await self.push_frame(frame, direction)
            return

        # --- Everything else passes through unchanged ---
        await self.push_frame(frame, direction)
