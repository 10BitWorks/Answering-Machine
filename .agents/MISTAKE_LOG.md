# Jitter Buffer Misdiagnosis (Sept 14)

On Sept 14, an agent erroneously diagnosed the audio skips in the Twilio streams as being caused by UDP packet loss, buffer starvation, or incorrect Twilio chunking (`audio_out_10ms_chunks=4`). It built a wall-clock `JitterBufferProcessor` to combat "Gemini's bursty delivery".

However, the actual symptom (as confirmed by the user) was that chunks of audio were **completely deleted** mid-word, forming a hard splice (phase discontinuity) without any silence or dropout gaps. This proved that audio was being systematically deleted from the pipeline, not delayed or starved.

The true root cause was a bug in Pipecat's `soxr_stream_resampler.py`. The resampler had a hardcoded `CLEAR_STREAM_AFTER_SECS = 0.2`. Whenever Gemini Live paused for >200ms to generate the next chunk of a sentence (a common occurrence with streaming LLMs), Pipecat would call `_soxr_stream.clear()`. This instantly discarded the resampler's internal filter delay buffer, which contained the actual pending audio samples, thus deleting 50-100ms of audio mid-word.

Ironically, the agent's new `JitterBufferProcessor` made the bug worse on `main`: by holding frames and flushing them all at once every 1000ms, it ensured that the time between calls to the resampler was always >200ms, causing the resampler to dump its buffer (and thus drop audio) exactly once per second, resulting in 16,564 phase discontinuities in a 2.5-minute call.
