# Progress: `start_turn` experiment evaluation + AGENTS.md cleanup

**Date:** 2026-07-30
**Status:** Discussion complete; AGENTS.md corrected; experiment not yet implemented.

---

## What triggered this

Evaluation of the "Future experiment" in `TODO.md`: a single every-turn `start_turn` tool that would replace `update_call_summary`, augment realtime status display, and give the model a sanctioned chain-of-thought outlet. Fields: `unfinished_speech`, `caller_said`, `audio_unclear`, `reaction_thoughts_plan`, `reply_expected`.

## Key finding: Gemini 3.1 Live function calling is synchronous/blocking by design

Verified against `reference/gemini-live-docs/gemini_live_tool_use.md` and `3.1-flash-and-migration.md`:

- **Docs say (three times):** "The model will not start responding until you've sent the tool response." Function calling on 3.1 is **synchronous only**.
- **No async escape hatch on 3.x:** Unlike Gemini 2.5 Flash Live, there is no `NON_BLOCKING` behavior declaration and no `scheduling` (`WHEN_IDLE`/`INTERRUPT`/`SILENT`) response hint. Confirmed in Pipecat's `_supports_non_blocking_tools` (returns `False` for any model containing `"gemini-3"`).
- **Terminology insight:** Google's docs consistently say "synchronous," never "blocking." Likely deliberate — two competing hypotheses for *why*:
  - **H1 (trained to wait):** The server keeps running; the model is trained to wait. Explains occasional violations and why Google avoids "blocking."
  - **H2 (output-gated, narrow window):** The model genuinely blocks but only the speech-output channel, possibly only during a narrow window at turn onset. Explains why violations are rare (most tools are instant; the blocking window is small).
  - Both hypotheses make `start_turn` safe; practical guidance is identical. H1 vs. H2 is unresolved but doesn't block the experiment.

## Production log verification (Jul 30, 2026)

Analyzed all 28 prod calls (Jul 23–30) + all dev calls. Extracted every `Bot started speaking` / `Calling function` / `Sending tool result` event and checked for blocking violations (bot speaking while a tool result was still in flight).

**Result: Zero violations.** In 100% of tool interactions, the sequence was: bot stops speaking → tool call fires → result sent → bot starts speaking. The model never started speaking before a pending result was delivered.

**Context from the project owner:** Violations WERE observed earlier in development when tools had noticeable delays (slow network requests). The `ask_support_bot` instant-return workaround (§2) exists specifically because of this — slow tools violated the blocking contract, causing self-interrupts and restarts. Once all tools were made instant, violations stopped. The blocking contract is reliable when tools respect it by returning fast.

**Other log findings:**
- Parallel tool calls confirmed (§16): model fires multiple tools simultaneously (`update_call_summary` + `transfer_call`, `lookup_contact` × 2, etc.). Blocking still holds — all results sent before speech resumes.
- §2 workaround working as designed: `ask_support_bot` returns instantly with `{"status": "processing", "instruction": "say 'Let me check on that'..."}`.
- §7 sentinel guard gap: non-speech sound triggered VAD → transcribed as "hold your breath" → model created contact record for "Hold your breath" (first/last name). Guard rejects "Unknown"/"Caller"/"Anonymous" but not random phrases. Relevant to `start_turn`'s `audio_unclear` field.
- Transcription inaccuracy confirmed: official transcript shows `"Are the Muppet made?"` while the Live model responded coherently. Validates the experiment's premise that `caller_said` (from the model's understanding) is more accurate than the Pipecat transcript.

## Implication for the `start_turn` experiment

The experiment is **safer than initially assessed, and now empirically grounded.** The log verification (above) confirmed the blocking contract holds in 100% of production calls with instant-return tools. `start_turn` with an instant result → model receives result before speaking → first response preserved, no self-interrupt. The first-vs-second-response concern is largely moot for instant tools.

Design refinements agreed:
- Tool description should include a strong **"do not speak until you have the result"** directive (prompt-level steering; the only available lever on 3.x since there's no API-level `WHEN_IDLE`).
- **Log `_bot_is_responding` at result-delivery time** to empirically verify the blocking contract holds per-turn. This directly tests the H1/H2 question with production data.
- `unfinished_speech` is retained as an **instrument** to test whether the model has any awareness of its own playback timing (hypothesis: full text is generated at LLM speed, audio plays back at human pace, model doesn't know what made it out before interruption).
- `reply_expected` + auto-prompt is feasible: the silence would be broken by a *specific* prompted utterance, not a content-less `LLMRunFrame` panic-trigger (different failure mode than the §6.1 concern).

## AGENTS.md changes made this session

| Section | Change | Basis |
|---|---|---|
| §1.3 Tool Result Insertion | **Rewritten** (was incorrectly rewritten mid-discussion, then corrected). Now: "Synchronous/Blocking by Design," citing the two reference docs. Adds terminology note (H1/H2), production caveat, edge-case handling for state-mutating tools. | `reference/gemini-live-docs/` + Pipecat source |
| §1.3 "Parallel vs. Sequential" / "Timing Relative to the Mic" | **Removed.** The "parallel vs sequential" framing didn't match observed behavior; the "who holds the mic" framing was a reaction to a since-corrected error. Durable guidance (double-fire risk on state-mutating tools) folded into §1.3 edge-case bullet. | — |
| §2 `cancel_on_interruption=False` | **Corrected.** No longer a fatal crash; it's a one-time warning + non-fatal error frame. Official async-tool example uses it. Practical guidance unchanged (use default on 3.x). | Pipecat source: `_process_completed_function_calls` |
| §2 "Timeouts" bullet | **Removed.** False claim that "Pipecat enforces a strict 5.0-second timeout" — it was the project's own configured timeout, not a Pipecat default. | Pipecat source: `function_call_timeout_secs` defaults to `None` |
| §3 "Avoid Infinite Silence" | **Removed.** Used the pre-1.5.0 parameter name (`audio_out_can_send_silence`); superseded by §11 which documents the rename to `audio_out_auto_silence`. | Internal inconsistency |
| §10 Call Summaries | **Added** "Always-Call Drag-Forward" bullet: forcing `update_call_summary` every turn dragged old resolved topics to the front of context, causing the bot to re-answer answered questions. Directive scaled back to "between topics" — solves re-answering but leaves short calls without summaries. Motivates part of the `start_turn` experiment. | Production observation |

## Open items (not blocking the experiment)

1. **`run_llm=False` claim (§1.3 warning):** Flagged as UNVERIFIED. Could not confirm against current Pipecat source whether it actually prevents the functionResponse from being sent. Needs re-test.
2. **H1 vs. H2:** Log data leans toward H2 (real blocking, reliable for instant tools) but doesn't definitively resolve the internal mechanism. Violations were only observed with slow tools (now eliminated). The `_bot_is_responding` logging in the `start_turn` experiment would confirm per-turn.
3. **Unverified-but-plausible AGENTS.md sections:** §4 (30-second EndFrame bug), §6 (`LLMRunFrame` panic-speak), §13 (webhook race), §16/§17 (parallel tool hallucinations, speech leaks). Behaviors were observed and workarounds are working; root-cause analyses may be flawed. Not touched.
4. **Pipecat source location:** Not vendored in the project; fetched from GitHub `main` during this session. The installed version in the Docker container may differ from `main`. Reference docs in `reference/pipecat-docs/` are a snapshot.

## What a future session should do next

- If implementing `start_turn`: start with the tool description carrying the "do not speak until result" directive, return instantly, and log `_bot_is_speaking` at result-delivery. Run for a week of calls, then review the log to see how often the blocking contract held.
- If verifying `run_llm=False`: test in the dev stack with a simple tool that returns `run_llm=False` and observe whether Gemini receives the functionResponse.
- If re-examining §4/§6/§13/§16/§17: SSH to `ubuntu@10bit.works`, check `/logs` in dev/prod containers (note: docker volume may not survive reboots; container rebuilds on every git sync).
