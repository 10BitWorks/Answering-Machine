

- Get steering hint field in Slack to work
- Get actual AI cost from Google AI API for Slack display

- Let recording upload wait indefinitely since transferred calls continue on after bot ends and recording doesn't arrive until final hangup
- Use audio-aware gemini LLM to summarize actual final audio clip

- Remove / Rethink Zammad integration
- Improve metadata and formatting of CiviCRM call Activities
- Get external support bot set up with quick responses
- Use wiki.10bitworks.org as knowledgebase


# Future experiment

Experiment with single every-turn tool to replace summaries, augment realtime status display, and allow model thinking:

`start_turn` tool - take realtime notes. ALWAYS call when it is your turn to speak. Returns immediately, but WAIT TO SPEAK until you get the results.

unfinished_speech: string 
// the remainder of your last response that you didn't get to say, if you were interrupted. If you were not interrupted, leave blank.

caller_said: string
// transcribe exactly what the caller said, as you heard it.

audio_unclear: boolean
// only true if you are not fully confident in your transcription due to issues with audio quality, audio volume, noise cancellation, or caller's accent.

reaction_thoughts_plan: string
// your reaction to what they said, what you think about it based on your knowledge and instructions, and the reasoning behind the way you plan to respond

reply_expected: boolean
// whether you expect the caller to immediately speak back (e.g. `true` if you asked a question), or `false` if silence is appropriate (e.g. they're waiting on you to do some work). If true, the caller has 10 seconds after you finish speaking to respond before you will be automatically prompted to ask if they heard you properly.