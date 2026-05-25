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
*   **Tool Result Insertion (The Sync Deadlock)**: Gemini 3.1 Live is a stateful WebSocket protocol. Once a `functionCall` is emitted, the Google server-side state machine **blocks** until it receives a `functionResponse`.
    *   *Requirement*: You MUST always return a tool result to the LLM. 
    *   *Warning*: Do **NOT** use `run_llm=False` for tool results in this project. It prevents Pipecat from sending the mandatory response to Google, which causes the bot to hang in silence indefinitely.
*   **Parallel vs. Sequential Tool Calls**: Gemini may emit tool calls **simultaneously** with audio (Parallel mode) or **before** audio (Sequential mode). 
    *   *Hallucination Risk*: If a tool result arrives while the bot is already speaking its Parallel-mode answer, Gemini re-analyzes the context and often "restarts" its turn, tacking on hallucinated goodbyes or hangups.
    *   *Requirement*: Tracking/Background tools must use the **Hybrid Speech-Aware Strategy**: Wait for the bot to finish speaking before returning the result if it's already in a turn, but return immediately if it's waiting for the data.
*   **Voice Rotation**: The assistant must randomly select a voice for each incoming call from the full set of 30 Gemini Live personas:
    *   *Voices*: Zephyr, Puck, Charon, Kore, Fenrir, Leda, Orus, Aoede, Callirrhoe, Autonoe, Enceladus, Iapetus, Umbriel, Algieba, Despina, Erinome, Algenib, Rasalgethi, Laomedeia, Achernar, Alnilam, Schedar, Gacrux, Pulcherrima, Achird, Zubenelgenubi, Vindemiatrix, Sadachbia, Sadaltager, Sulafat.
    *   *Implementation*: Select the voice at the start of the `websocket_endpoint` and inject it into the `GeminiLiveLLMService` settings. Log the selected voice in the per-call log file.

## 2. Tool Calling with Gemini 3.1 Live
*   **No Explicit `cancel_on_interruption=False`**: Current Pipecat versions explicitly reject `cancel_on_interruption=False` for Gemini Live models (it raises a fatal `ErrorFrame`). The framework now handles async tool result delivery internally via its built-in `async_tool_cancellation` mechanism. Use the default (`cancel_on_interruption=True`, or omit the parameter) for all `register_function` calls. Pipecat still ensures the mandatory `functionResponse` is sent to the Gemini WebSocket.
    *   *Workaround*: If a tool needs to perform a network request (e.g., posting to Slack), the tool handler *must* return a success payload to the LLM immediately (via `await params.result_callback(...)`) and dispatch the actual work to a background task (e.g., `asyncio.create_task(send_to_slack(...))`). This prevents the bot from "stuttering" or hanging while waiting for the tool to finish.
*   **Separation of Lookup and Action**: Do not combine information gathering (lookup) and state mutation (e.g., transferring a call) into a single tool. Gemini may speculatively call a tool multiple times to gather options for the user.
    *   *Example*: Use `lookup_contact` to get a phone number, then require the bot to explicitly call `transfer_call` after presenting options to the user.
*   **Immediate Hangup Prompts**: To ensure the bot hangs up in the same conversational turn as its farewell, the tool description for `end_call` must include strong directives (e.g., "CRITICAL: You MUST call this tool immediately in the exact same turn after you say goodbye...").
*   **Timeouts**: Wrap all external network requests (like CiviCRM lookups) in an `asyncio.wait_for` timeout (e.g., 4.5 seconds). Pipecat enforces a strict 5.0-second timeout for tool execution, and catching it at 4.5s allows us to gracefully return an error to the LLM instead of crashing the pipeline. Also, pass `timeout_secs=5.0` to `llm.register_function`.

## 3. Twilio Audio Pacing and Stability
*   **Strict 20ms Pacing**: Twilio Media Streams expect 8kHz, 16-bit mono audio (320 bytes = 20ms chunks). Use Pipecat's **`fixed_audio_packet_size=320`** setting in `FastAPIWebsocketParams`. This ensures perfectly timed packets without the overhead of higher-level chunking.
*   **Avoid Chunks and Schedulers**: Do **NOT** use `audio_out_10ms_chunks`, `FixedSizeScheduler`, or `prefatory_silence_threshold`. These can interfere with the native framing and cause "clicking" or "speed-run" distorted audio.
*   **Avoid Infinite Silence**: Do NOT set `audio_out_can_send_silence=True`. While intended to keep the stream synchronized, it was proven to cause **massive audio dropouts and multi-second delays**. The "extra" silence frames compete with real audio frames at the Twilio gateway and interfere with the jitter buffer. `uvloop` provides enough performance to maintain 20ms pacing without this dangerous hack.

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
