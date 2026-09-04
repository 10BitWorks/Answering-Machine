# Phone Assistant Project Guidelines & Lessons Learned


Any message I send you will contain one or more (but usually just one) of the following types of communication. The type determines what you may do in your next turn, in response to that piece of the message:
1. CHAT: Asking a question about what you've done, asking about what's been said in the conversation, or making a correction to your statements.
For this type of message from me, in your next turn you may NOT make any code changes or call any tools. Just answer.
2. INVESTIGATION: Asking a question about the code, the logs, system status - or making an observational statement that contradicts your perspective, reporting a failure, etc.
For this type of message from me, in your next turn you may NOT make code changes, and you may ONLY call read-only tools and commands that do not affect the state of the system in any way. If you propose a plan at the end and ask to execute it, I may follow up with another INVESTIGATION - lack of disapproval does not imply approval.
3. EXECUTION: Telling you to do something like implement a plan, change git branches, run tests, etc.
For this type of message from me, in your next turn you may ONLY call tools and commands that modify system state that are strictly within the scope of the agreed plan - you may NOT decide to autonomously implement a workaround, or act based on guesses when the plan isn't working out. If you can't do it the way you said or implied you would, report back and wait for approval of your new plan.
4. VIOLATION: Telling you that you've broken one of these rules.
You may attempt to explain yourself, or suggest how the rules could be clearer, or suggest a recovery plan - but you may not go straight to work until approved.


This file documents critical architectural decisions, workarounds, and gotchas discovered during the development of the 10BitWorks Phone Assistant using Pipecat, Twilio, and the Gemini 3.1 Live API. Future agents modifying this codebase must adhere to these guidelines to prevent regressions.

I will modify the SYSTEM_PROMPT file myself. You may suggest, but don't touch it.

## 1. Gemini 3.1 Live Integration Constraints
*   **Model String**: The correct model string for Gemini 3.1 Live is `gemini-3.1-flash-live-preview`. Do **NOT** use the `models/` prefix. Using the prefix or incorrect strings will cause a `1011 Internal error` during connection.
*   **API Versioning**: Do not force the `v1alpha` API version or use `enable_affective_dialog=True` in the `GeminiLiveLLMService.Settings`. These features are specific to Gemini 2.5 Native Audio models. Stick to the default `v1beta` (which Pipecat uses automatically when `http_options` is omitted).
*   **Dynamic Prompting (Date/Time)**: Inject dynamic data (like the current time) into the initial **`developer`** role message using `context.add_message()`. 
    *   *Handshake Warning*: Do **NOT** use `llm.update_settings()` during the `on_client_connected` event; it triggers a session reset that causes the bot to remain silent.
    *   *Role Mapping*: Note that Pipecat maps the `developer` role to the `user` role in the Gemini Live history. To prevent the model from thinking the *caller* is providing these instructions, always prefix the message text with `SYSTEM INFO:` or `INSTRUCTION:`.
*   **Tool Result Insertion (Synchronous/Blocking by Design)**: Per Google's docs (`reference/gemini-live-docs/gemini_live_tool_use.md`, `3.1-flash-and-migration.md`), function calling on Gemini 3.1 Flash Live is **synchronous only** — "The model will not start responding until you've sent the tool response." Unlike Gemini 2.5 Flash Live, there is no `NON_BLOCKING` behavior declaration or `scheduling` (`WHEN_IDLE`/`INTERRUPT`/`SILENT`) response hint — the async escape hatch does not exist on 3.x (confirmed in Pipecat's `_supports_non_blocking_tools`, which returns `False` for any model containing `"gemini-3"`).
    *   *Terminology Note*: Google's docs consistently say "synchronous," never "blocking." This may be deliberate — the server may keep running while the model is trained to wait (H1), or only the speech-output channel may be gated with a narrow window at turn onset (H2). Either way, the observable contract is: no speech until the tool response is sent. Pipecat's `BLOCKING`/`NON_BLOCKING` terminology is its own, borrowed for cross-service consistency; it doesn't map cleanly to 3.x and shouldn't be read as evidence of how Google's server actually works.
    *   *Production Caveat*: "Synchronous" is model behavior, not a hard protocol halt. Verified across 28 production calls (Jul 2026): with instant-return tools, the blocking contract held in 100% of cases — the model never started speaking before the tool result was sent. Violations WERE observed earlier in development when tools had noticeable delays (slow network requests): the model would start speaking before the result arrived, then self-interrupt and restart its turn when the result landed (sometimes off-topic, sometimes tacking on hallucinated goodbyes/hangups). This is why ALL tools in this project now return instantly — slow work is dispatched to background tasks (see §2 workaround; `ask_support_bot` returns a "processing" instruction immediately while the real lookup happens in the background). The synchronous contract is reliable as long as tools respect it by returning fast.
    *   *Requirement*: You MUST always return a tool result. If you don't, the model will wait indefinitely (this indefinite-wait behavior is itself evidence that the synchronous contract is real and server-enforced, not merely a model suggestion).
    *   *Edge-Case Handling (State-Mutating Tools)*: For tools that mutate state or trigger side effects (transfer, Slack post), a blocking violation that lands the result mid-speech can cause the model's turn-restart to re-fire the tool (double transfer, double post). As a defensive measure, delay returning the result until the bot has finished speaking if it's already in a turn. For instant/lookup tools, return immediately — the synchronous contract handles it.
    *   *Warning (UNVERIFIED — needs re-test against current Pipecat)*: A previous version of this document claimed `run_llm=False` "prevents Pipecat from sending the mandatory response to Google, which causes the bot to hang in silence indefinitely." This could not be confirmed against current Pipecat source and may be stale. Re-test before relying on it.
*   **Voice Rotation**: The assistant must randomly select a voice for each incoming call from the full set of 30 Gemini Live personas:
    *   *Voices*: Zephyr, Puck, Charon, Kore, Fenrir, Leda, Orus, Aoede, Callirrhoe, Autonoe, Enceladus, Iapetus, Umbriel, Algieba, Despina, Erinome, Algenib, Rasalgethi, Laomedeia, Achernar, Alnilam, Schedar, Gacrux, Pulcherrima, Achird, Zubenelgenubi, Vindemiatrix, Sadachbia, Sadaltager, Sulafat.
    *   *Implementation*: Select the voice at the start of the `websocket_endpoint` and inject it into the `GeminiLiveLLMService` settings. Log the selected voice in the per-call log file.

## 2. Tool Calling with Gemini 3.1 Live
*   **`cancel_on_interruption=False` on Gemini 3.x**: Pipecat no longer **crashes** on `cancel_on_interruption=False` for Gemini Live — current versions log a one-time error and push a non-fatal error frame, then continue. The official async-tool example (`examples/realtime/realtime-gemini-live-async-tool.py`) uses `@tool_options(cancel_on_interruption=False)` explicitly. **However**, on Gemini 3.x the async semantics are structurally unachievable: Pipecat would normally map this flag to a `NON_BLOCKING` tool declaration + `scheduling: WHEN_IDLE` response hint, but 3.x supports neither (see `_supports_non_blocking_tools` and `_process_completed_function_calls` in `src/pipecat/services/google/gemini_live/llm.py`). The framework detects this and emits a warning.
    *   *Practical Guidance*: Keep `cancel_on_interruption=True` (the default) for all `register_function` calls on Gemini 3.x. If you need fire-and-forget semantics, fake them yourself (see workaround).
    *   *Workaround*: If a tool needs to perform a network request (e.g., posting to Slack), the tool handler *must* return a success payload to the LLM immediately (via `await params.result_callback(...)`) and dispatch the actual work to a background task (e.g., `asyncio.create_task(send_to_slack(...))`). This prevents the bot from "stuttering" or hanging while waiting for the tool to finish.
*   **Separation of Lookup and Action**: Do not combine information gathering (lookup) and state mutation (e.g., transferring a call) into a single tool. Gemini may speculatively call a tool multiple times to gather options for the user.
    *   *Example*: Use `lookup_contact` to get a phone number, then require the bot to explicitly call `transfer_call` after presenting options to the user.
*   **Immediate Hangup Prompts**: To ensure the bot hangs up in the same conversational turn as its farewell, the tool description for `end_call` must include strong directives (e.g., "CRITICAL: You MUST call this tool immediately in the exact same turn after you say goodbye...").

## 3. Twilio Audio Pacing and Stability
*   **Strict 20ms Pacing**: Twilio Media Streams expect 8kHz, 16-bit mono audio (320 bytes = 20ms chunks). Use Pipecat's **`fixed_audio_packet_size=320`** setting in `FastAPIWebsocketParams`. This ensures perfectly timed packets without the overhead of higher-level chunking.
*   **Avoid Chunks and Schedulers**: Do **NOT** use `audio_out_10ms_chunks`, `FixedSizeScheduler`, or `prefatory_silence_threshold`. These can interfere with the native framing and cause "clicking" or "speed-run" distorted audio.

## 4. Graceful Shutdown & The 30-Second Gemini Bug
*   **The Bug**: Gemini 3.1 Live defers the processing of `EndFrame` for 30 seconds after the bot finishes speaking, which causes the Twilio call to hang in silence if a standard `EndTaskFrame` is used.
*   **The Fix (Wait-and-Cancel)**:
    1.  Use a custom `FrameProcessor` (e.g., `SpeechTracker`) that intercepts `BotStartedSpeakingFrame` and `BotStoppedSpeakingFrame` to accurately track when the bot is outputting audio.
    2.  When a tool like `end_call` or `transfer_call` is triggered, launch a background task (`wait_and_terminate`).
    3.  This task must poll the `SpeechTracker` and wait for a contiguous period of silence (e.g., 1.5 seconds) to ensure the bot's trailing "room tone" has fully flushed to Twilio.
    4.  Finally, push a `CancelTaskFrame()` UPSTREAM to immediately kill the pipeline and bypass the 30-second Gemini hang.
*   **Twilio Studio Fallback**: To allow Twilio to fall back to a Studio Flow `<Redirect>` after the bot hangs up or transfers:
    *   Set `auto_hang_up=False` in the `TwilioFrameSerializer`. This prevents Pipecat from terminating the call via the REST API.
    *   Maintain a global state (e.g., `pending_hangups`, `pending_transfers`).
    *   When the WebSocket closes (due to the `CancelTaskFrame`), Twilio hits your `/post_bot` TwiML endpoint. Check the global state there to return either a `<Dial>` or a `<Hangup>`/`<Redirect>`.

## 5. Scope and Initialization
*   Always define tool handlers and background tasks *after* the `llm` and `transport` objects are fully instantiated within the async endpoint. Defining them beforehand leads to `UnboundLocalError` crashes because the closure attempts to capture variables that aren't fully initialized.
*   **Zero-Tolerance for Broken Code**: Never push code to the repository without running a compilation check (`python3 -m py_compile <file>`) to verify syntax and imports. 

## 6. Context Management & Prompt Injection
*   **Avoid Aggressive `LLMRunFrame`**: When injecting silent warnings or background context updates into the conversation history (e.g., a "time remaining" warning), do **NOT** queue an `LLMRunFrame()` immediately afterward. `LLMRunFrame()` mechanically forces the LLM to generate speech *right now*. If the bot has nothing conversational to say, this will cause it to abruptly interrupt the caller or "panic-speak" hallucinated text (like reciting random parts of its prompt). Simply use `context.add_message(...)` and let the bot naturally see the new context during its next regular conversational turn.
*   **Caller Profile Injection**: For recognized contacts, the `on_client_connected` handler fetches a full CiviCRM profile (membership status with join/start/end dates, all addresses, phones, and emails) and injects it into a `CURRENT CALLER INFO` block in the initial developer prompt. This allows the bot to answer personal account questions instantly without tool calls.

## 7. CiviCRM Contact Management
*   **Create Contact**: The bot proactively creates new contact records for unrecognized callers using the `create_my_contact_record` tool as soon as they provide a first and last name. This ensures all inquiries are accurately logged in CiviCRM.
*   **Sentinel Name Guard**: The `create_contact_handler` rejects names like "Unknown", "Caller", "Anonymous", etc. to prevent Gemini from creating bogus records when it misinterprets context text as a caller's name.
*   **Safe Updates**: Data management tools (address, phone, email) are "add-only" or "primary-toggle" to prevent accidental deletion or overwriting of existing records. The bot cannot delete records.
*   **Membership Intelligence**: Membership info now includes `join_date` and `start_date` alongside `end_date`, providing the bot with full context on the user's history with the makerspace.

## 8. Caller Identification & Greeting
*   **CNAM Handling**: Twilio CNAM arrives in ALL CAPS (e.g. "DAVID BLUM"). The code title-cases it and extracts the first name for the greeting ("Am I speaking with David?"), while passing the full name in the context `detail_block` for the bot's reference.
*   **Unknown Callers**: When neither CiviCRM nor CNAM identifies the caller, the `detail_block` is left empty — do NOT inject "Unknown Caller" or similar sentinel text that Gemini might interpret as a name.
*   **Transcript Labels**: Use `caller_recognized_name` (CiviCRM first name) over `caller_name` (CNAM) over "Caller" for transcript speaker labels.
*   **Transfer Phone Guard**: The `transfer_call_handler` rejects phone numbers containing "555" to prevent Gemini from dialing hallucinated placeholder numbers when a `lookup_contact` call gets cancelled by user interruption.
*   **Time Limit**: The SYSTEM_PROMPT does NOT mention call time limits. Time warnings are injected programmatically via `session_warning_task` at 7, 8, and 9 minutes only.



## 9. Deployment & CI/CD
*   **Auto-Deployment**: The production stack (`call-bot` on port 17293) is configured to auto-update and redeploy automatically whenever changes are pushed to the `main` branch. The dev stack does the same on the `dev` branch.

## 10. Call Summaries
*   **Live Summary via Tool Call**: The bot updates a running call summary by calling the `update_call_summary` tool between topics (NOT every turn — see "Always-Call Drag-Forward" below). The summary is stored in the global `call_summaries` dict keyed by `call_sid`.
*   **No Separate AI**: Do NOT use a secondary AI model/API call to generate summaries. The same Gemini Live session that handled the call produces the summary during the conversation.
*   **Abrupt Hangup Coverage**: Because the summary is updated incrementally during the call, even if the caller hangs up abruptly, the most recent summary is already available for the webhook.
*   **Webhook Integration**: The `/recording-callback` endpoint reads from `call_summaries` and includes it in the Slack webhook payload under the `"Summary"` key.
*   **Always-Call Drag-Forward (context pollution)**: An earlier version of the `update_call_summary` tool description instructed the model to "ALWAYS call this after every turn." The model complied — but because the summary includes conversation history, this dragged old, already-resolved topics to the front of the context, *ahead of* even what the user had just said. The bot then began re-answering questions that had already been answered. The directive was scaled back to "call between topics" only, which solved the re-answering problem but means short caller-ended calls may terminate without any summary available. Any future tool that asks the model to summarize mid-conversation should be evaluated for this same drag-forward risk.

## 11. Pipecat Parameter Renames
*   **`audio_out_auto_silence`**: In Pipecat 1.5.0, the parameter `audio_out_can_send_silence` was renamed to `audio_out_auto_silence` in `FastAPIWebsocketParams`. Using the old name silently falls back to the default (`True`), which causes audio dropouts. Always use `audio_out_auto_silence=False`.

## 12. Transcript Ordering and VAD Limitations
*   **No Turn Frames**: Gemini 3.1 Live does not emit `UserStoppedSpeakingFrame` (VAD turn frames). As a result, Pipecat's `LLMUserContextAggregator` falls back to realtime mode and drops its turn strategies, meaning `context.messages` gets populated arbitrarily and out-of-order during interruptions.
*   **Chronological Tracking**: To capture the exact chronological ordering of live speech, a custom `SpeechTracker` MUST intercept `TranscriptionFrame`s **UPSTREAM** (before the aggregator consumes them) and `BotStartedSpeakingFrame`s **DOWNSTREAM**. If the tracker is placed at the bottom of the pipeline, it will miss the upstream user transcriptions entirely.

## 13. The Webhook Race Condition
*   **Twilio vs. Pipecat Teardown**: When a caller hangs up abruptly (especially while the bot is speaking), Twilio immediately fires the `/post_bot` and `/recording-callback` webhooks. However, Pipecat may not catch the disconnect immediately and will stay alive until the 120s `idle_timeout`.
*   **The Fix**: If the `/recording-callback` webhook fires and tries to fetch the transcript while Pipecat is still alive, it will send an empty transcript. The webhook endpoints must manually pop the `task` from the `active_calls` dictionary and inject a `CancelTaskFrame()` to force Pipecat to tear down instantly and flush the transcripts.

## 14. Negative Rule Inversion (The "Safe Assumptions" Loophole)
*   **Eager to Please**: Because LLMs are eager predictive text engines, providing strict negative instructions with specific conditions (e.g., "If X is atypical, assume we don't have it") will often be logically inverted by the LLM ("Since X IS typical, I CAN assume we DO have it!").
*   **Cascading Hallucinations**: Once the LLM assumes a craft exists (e.g., "painting"), it will confidently hallucinate the standard equipment for that craft (e.g., "ventilated spray booth"). Negative constraints must be absolute (e.g., "Never assume we have it, even if it is typical").

## 15. The Interruption Resumption Quirk
*   **Stubborn Resumption**: If the bot goes on a tangent (e.g., selling the caller on an unrelated feature from the KB) and the user interrupts with a noise (like "Ah!"), the LLM will often stubbornly resume and finish its interrupted sentence rather than acknowledging the context of the user's interruption. Prompt engineering must forbid the bot from listing random unprompted features.

## 16. Parallel Tool Call Hallucinations
*   **Asking and Executing Simultaneously**: Gemini Live will sometimes decide to ask a clarification question (e.g., "Would you like me to transfer you to Beans?") and *simultaneously* emit the state-mutating tool (`transfer_call`) in the exact same generation step!
*   **The Fix**: Tools that mutate state MUST have explicit instructions forbidding them from being called in the same turn as offering the action (e.g., "You must wait for their 'yes' or 'no' response in a separate conversational turn BEFORE calling this tool").

## 17. Tool Call Leaks in Speech
*   **Internal Reasoning Leak**: Because Gemini Live is a unified audio model, it sometimes leaks internal function call reasoning (e.g., reciting `response:ask_support_bot{result:...}`) into its spoken `TextFrame` output stream if a tool fails or if it decides to simulate a tool response.
*   **The Fix**: The raw text stream within the `SpeechTracker` must be scrubbed of these regex patterns before final transcript logging.

## 18. Critical Pipeline Topology & Coding Pitfalls
*   **TranscriptionFrame Consumption**: `TranscriptionFrame`s flow downstream from the transport/STT and are completely consumed by the `LLMUserContextAggregator`. Any custom `SpeechTracker` or logging processor MUST be placed **before** (`user_aggregator`) in the pipeline array. If placed after the LLM, it will never see the user's speech, leading to completely one-sided transcripts.
*   **Native Audio Models vs TextFrames**: Native multimodal models (like `GeminiLiveLLMService`) do not use a separate TTS engine. They output `AudioRawFrame`s directly and **NEVER** emit `TextFrame`s downstream! If you attempt to capture the bot's speech by intercepting `TextFrame`s in a custom processor, your variables will remain silently empty.
*   **The Dropped Interrupted Context**: When the bot is interrupted, Pipecat's context aggregator completely drops the text buffer. Relying on `LLMContextFrame` to capture the bot's speech means you will silently lose all apologies or partially-spoken sentences that were interrupted.
*   **Webhook Pipeline Cancellation Pattern**: The correct code pattern to force a Pipecat pipeline to tear down instantly when an external webhook (like Twilio's `/recording-callback`) signals the call is over is to store the `PipelineTask` globally (e.g., in `active_calls[call_sid]["task"]`), then have the webhook endpoint inject `asyncio.create_task(task.queue_frames([CancelTaskFrame()]))`. This forcefully bypasses any `audio_out_auto_silence` delays or `idle_timeout` hangs.

## 19. `call_data` Dictionary Structure
*   **Nested `body` Dict**: Twilio metadata (`destination_number`, `caller_number`, `caller_name`, `caller_city`, `caller_state`, `caller_zip`) is nested under `call_data["body"]`, not at the top level. The top level contains only `call_id`, `stream_id`, and `body`. Any code accessing caller/destination fields must use `call_data.get("body", {}).get("field_name", "")`.
*   **Historical Bug**: A guard rail in `transfer_call_handler` used `call_data.get("destination_number")` (top-level), which always returned `""`, silently disabling the self-transfer check. This allowed the bot to transfer callers to the bot's own phone number, creating an infinite loop where the caller was forced to restart the conversation from scratch. Fixed Aug 2026.

## 20. Call Log Investigation Protocol
*   **Before Investigating**: Read `SYSTEM_PROMPT.md` (which includes the full knowledge base from `knowledgebase/`) to understand the bot's intended behavior before judging whether a call went well or poorly.
*   **Quick Log Access**: Production call logs are on the host filesystem at a stable path (not inside the container, which has a random name that changes on each deploy):
    ```
    ssh ubuntu@10bit.works "cat /data/coolify/applications/o5lajf9uajiwd8ntwtge2siz/logs/call_<CALL_SID>.log"
    ```
    To list recent calls: `ssh ubuntu@10bit.works "ls -lt /data/coolify/applications/o5lajf9uajiwd8ntwtge2siz/logs/ | head -n 10"`
*   **Cross-Reference Phone Numbers**: For every `transfer_call` tool invocation in the logs, compare the `phone_number` argument against the bot's own `destination_number` from the `Accepted twilio call` line. If they match, the bot attempted a self-transfer.
*   **Detect Looped Callers**: Look for multiple `Accepted twilio call` lines with the same `caller_number` in rapid succession (seconds apart). This means the caller was transferred back to the bot and forced to restart.
*   **Listen Before Theorizing**: When asked to review a call recording, describe what you hear chronologically across the full recording *before* forming any theory. Do not invent observations — if you aren't sure what was said, say so.

## 21. Multi-Session Recordings
*   **One Recording, Multiple Bot Sessions**: A single Twilio recording can span multiple bot sessions if the caller gets transferred back to the bot (e.g., via a self-transfer or a Studio flow redirect after a failed dial). The caller never hung up, so Twilio kept recording.
*   **Signs**: A second greeting mid-recording, a different bot voice (each session picks a random voice), and a new `call_id` in the logs from the same `caller_number` appearing seconds after a `transfer_call` or `post_bot` entry.
*   **Root Cause**: This almost always means the bot transferred the caller to its own number (see §19) or a Studio flow redirect looped back to the bot after a failed `<Dial>`.

## 22. Jitter Buffer (The 2x Send Rate Problem)
*   **Root Cause**: The FastAPI WebSocket transport's `_send_interval` is `(audio_chunk_size / sample_rate) / 2` — it sends audio at **2x real-time**. With default `audio_out_10ms_chunks=4` and `sample_rate=8000`, each 640-byte chunk (80ms of audio) is paced with only a 40ms sleep. This rapidly drains any queue backlog. When Gemini's audio generation stutters for even a few milliseconds, the queue starves and Twilio receives no packet, producing audible drops mid-speech.
*   **Why AI Studio Doesn't Have This**: Google's AI Studio uses WebRTC, which has a native jitter buffer. Our Twilio WebSocket pipeline has none, so the same slightly-uneven audio stream from Gemini sounds smooth in AI Studio but stuttery over the phone.
*   **The Fix**: A `JitterBufferProcessor` in `processors.py` buffers the first 200ms of each bot utterance before releasing it to the transport. The transport's 2x-speed pacing loop then takes ~200ms to drain this burst, during which Gemini continues generating audio — building a natural backlog that acts as a continuous jitter buffer for the rest of the utterance.
*   **Implementation**: The processor has three states: `WAITING` (no audio yet), `BUFFERING` (accumulating initial frames), and `PASSTHROUGH` (threshold met, frames flow directly). It resets on `TTSStoppedFrame` or `InterruptionFrame`, flushing any partial buffer first to avoid losing audio.
*   **Pipeline Position**: Must be placed immediately before `transport.output()` in the pipeline array (after `metrics_logger`).
*   **Do NOT use `audio_out_10ms_chunks`**: The §3 guidance still applies — combining higher `audio_out_10ms_chunks` with `fixed_audio_packet_size=320` causes the transport to burst all sub-packets at once and then sleep for the full chunk duration, which overwhelms Twilio's small buffer. The jitter buffer approach is the correct fix.
