import asyncio
from pipecat.processors.frame_processor import FrameProcessor, FrameDirection
from pipecat.frames.frames import (
    Frame,
    BotStartedSpeakingFrame,
    BotStoppedSpeakingFrame,
    TranscriptionFrame,
    LLMContextFrame,
    AudioRawFrame
)

class SpeechTracker(FrameProcessor):
    """
    Custom Pipecat FrameProcessor for tracking user and bot speech events,
    VAD status, chronologically recording call history transcripts, and
    structuring turn tasks for real-time Slack updates.
    """
    def __init__(self, call_history: list = None, on_turn_update = None):
        super().__init__()
        self.is_speaking = False
        self.first_utterance_finished = False
        self.call_history = call_history if call_history is not None else []
        self.tasks = []  # List of turn dicts for Slack plan block schema
        self.on_turn_update = on_turn_update
        self.current_task = None

    def add_task_detail(self, detail_text: str):
        """Adds internal tool/action detail to the active turn task."""
        if self.current_task:
            self.current_task["details_text"] = detail_text
            if self.on_turn_update:
                asyncio.create_task(self._trigger_update())

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
        if isinstance(frame, BotStartedSpeakingFrame):
            self.is_speaking = True
        elif isinstance(frame, BotStoppedSpeakingFrame):
            self.is_speaking = False
            self.first_utterance_finished = True
            if self.current_task and self.current_task.get("status") == "in_progress":
                self.current_task["status"] = "complete"
                await self._trigger_update()
        elif isinstance(frame, TranscriptionFrame):
            if frame.user_id == "user" and frame.text and frame.text.strip():
                text = frame.text.strip()
                self.call_history.append(f"[User] {text}")
                
                # Create a new turn task object matching Slack plan block task schema
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
        elif isinstance(frame, LLMContextFrame):
            for msg in reversed(frame.context.messages):
                if msg.get("role") == "assistant" and msg.get("content"):
                    bot_text = msg["content"].strip()
                    self.call_history.append(f"[Bot] {bot_text}")
                    if self.current_task:
                        self.current_task["output_text"] = bot_text
                        await self._trigger_update()
                    break
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
