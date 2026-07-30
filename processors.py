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
    VAD status, and chronologically recording call history transcripts.
    """
    def __init__(self, call_history: list = None):
        super().__init__()
        self.is_speaking = False
        self.first_utterance_finished = False
        self.call_history = call_history if call_history is not None else []

    async def process_frame(self, frame: Frame, direction: FrameDirection):
        await super().process_frame(frame, direction)
        if isinstance(frame, BotStartedSpeakingFrame):
            self.is_speaking = True
        elif isinstance(frame, BotStoppedSpeakingFrame):
            self.is_speaking = False
            self.first_utterance_finished = True
        elif isinstance(frame, TranscriptionFrame):
            if frame.user_id == "user":
                self.call_history.append(f"[User] {frame.text}")
        elif isinstance(frame, LLMContextFrame):
            for msg in reversed(frame.context.messages):
                if msg.get("role") == "assistant" and msg.get("content"):
                    self.call_history.append(f"[Bot] {msg['content']}")
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
