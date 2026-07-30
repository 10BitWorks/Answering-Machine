import os
import asyncio
import time
import httpx
from loguru import logger

# Active Slack sessions store: {call_sid: {"ts": str, "channel": str, "last_update": float, "caller_name": str, "crm_url": str, "summary": str, "tasks": list, "context": object, "speech_tracker": object}}
active_slack_sessions = {}

def get_slack_session_by_ts(ts: str) -> dict:
    """Find active call session matching a message ts."""
    for call_sid, session in active_slack_sessions.items():
        if session.get("ts") == ts:
            return session
    return None

def get_slack_session_by_channel(channel: str) -> dict:
    """Find most recently updated active call session matching a Slack channel."""
    matching = [s for s in active_slack_sessions.values() if s.get("channel") == channel]
    if matching:
        matching.sort(key=lambda s: s.get("last_update", 0), reverse=True)
        return matching[0]
    return None

def build_rich_text_object(text: str) -> dict:
    """Helper to wrap string text into Slack rich_text block element."""
    if not text:
        return None
    return {
        "type": "rich_text",
        "elements": [
            {
                "type": "rich_text_section",
                "elements": [
                    {
                        "type": "text",
                        "text": text
                    }
                ]
            }
        ]
    }

def build_during_call_blocks(call_sid: str, caller_name: str, summary: str, tasks: list, sources: list = None) -> list[dict]:
    """
    Builds Slack Block Kit payload matching during-call-example.json layout.
    """
    plan_tasks = []
    
    for idx, t in enumerate(tasks, 1):
        task_id = t.get("task_id", f"task_{idx}")
        title = t.get("title", f"Turn {idx}")
        status = t.get("status", "complete")
        
        task_obj = {
            "task_id": task_id,
            "title": title,
            "status": status
        }
        
        if t.get("details_text"):
            task_obj["details"] = build_rich_text_object(t["details_text"])
            
        if t.get("output_text"):
            task_obj["output"] = build_rich_text_object(t["output_text"])
            
        if t.get("sources"):
            task_obj["sources"] = t["sources"]
        elif sources and idx == 1:
            task_obj["sources"] = sources
            
        plan_tasks.append(task_obj)
        
    # Append pending task if last task is complete
    if not plan_tasks or plan_tasks[-1].get("status") == "complete":
        plan_tasks.append({
            "task_id": f"task_{len(plan_tasks)+1}_bkb",
            "title": "Listening for next response...",
            "status": "pending"
        })

    summary_text = summary if summary else f"Incoming call in progress from *{caller_name}*..."
    
    blocks = [
        {
            "type": "context",
            "elements": [
                {
                    "type": "mrkdwn",
                    "text": summary_text
                }
            ]
        },
        {
            "type": "plan",
            "plan_id": f"plan_{call_sid}",
            "title": f"Handling incoming call from {caller_name}",
            "tasks": plan_tasks
        },
        {
            "dispatch_action": True,
            "type": "input",
            "element": {
                "type": "plain_text_input",
                "action_id": "plain_text_input-action"
            },
            "label": {
                "type": "plain_text",
                "text": "Give me a hint",
                "emoji": True
            },
            "optional": False
        }
    ]
    return blocks

def build_after_call_blocks(call_sid: str, caller_name: str, duration_str: str, summary: str, tasks: list, sources: list = None) -> list[dict]:
    """
    Builds Slack Block Kit payload matching after-call-example.json layout.
    Ensures tasks list is never empty.
    """
    plan_tasks = []
    
    for idx, t in enumerate(tasks, 1):
        task_id = t.get("task_id", f"task_{idx}")
        title = t.get("title", f"Turn {idx}")
        
        task_obj = {
            "task_id": task_id,
            "title": title,
            "status": "complete"
        }
        
        if t.get("details_text"):
            task_obj["details"] = build_rich_text_object(t["details_text"])
            
        if t.get("output_text"):
            task_obj["output"] = build_rich_text_object(t["output_text"])
            
        if t.get("sources"):
            task_obj["sources"] = t["sources"]
        elif sources and idx == 1:
            task_obj["sources"] = sources
            
        plan_tasks.append(task_obj)

    # Fallback task if no tasks were recorded
    if not plan_tasks:
        plan_tasks.append({
            "task_id": "task_1",
            "title": "Call conversation completed",
            "status": "complete",
            "details": build_rich_text_object("Gracefully ended call")
        })

    summary_text = summary if summary else f"Call completed with *{caller_name}*."
    title_text = f"{duration_str} call from {caller_name}" if duration_str else f"Call from {caller_name}"

    blocks = [
        {
            "type": "context",
            "elements": [
                {
                    "type": "mrkdwn",
                    "text": summary_text
                }
            ]
        },
        {
            "type": "plan",
            "plan_id": f"plan_{call_sid}",
            "title": title_text,
            "tasks": plan_tasks
        }
    ]
    return blocks


async def start_live_call_slack_session(call_sid: str, caller_name: str, caller_number: str, crm_url: str = None, context=None, speech_tracker=None) -> str:
    """
    Posts the initial 'during call' Slack Block Kit message when a call connects.
    """
    slack_token = os.getenv("SLACK_BOT_TOKEN")
    channel_id = os.getenv("SLACK_CHANNEL_ID") or os.getenv("RECORDING_SLACK_CHANNEL_ID")
    if not (slack_token and channel_id):
        logger.info("SLACK_BOT_TOKEN or SLACK_CHANNEL_ID not set; skipping live Slack session.")
        return None

    sources = [{"type": "url", "url": crm_url, "text": f"CiviCRM contact ({caller_name})"}] if crm_url else None
    display_name = caller_name if caller_name else caller_number
    blocks = build_during_call_blocks(call_sid, display_name, "", [], sources=sources)

    async with httpx.AsyncClient(timeout=10.0) as client:
        headers = {"Authorization": f"Bearer {slack_token}"}
        try:
            res = await client.post(
                "https://slack.com/api/chat.postMessage",
                headers=headers,
                json={
                    "channel": channel_id,
                    "blocks": blocks,
                    "text": f"Handling incoming call from {display_name}"
                }
            )
            data = res.json()
            if data.get("ok"):
                ts = data.get("ts")
                active_slack_sessions[call_sid] = {
                    "ts": ts,
                    "channel": channel_id,
                    "last_update": time.time(),
                    "caller_name": display_name,
                    "crm_url": crm_url,
                    "summary": "",
                    "tasks": [],
                    "context": context,
                    "speech_tracker": speech_tracker
                }
                logger.info(f"Started live Slack session for call {call_sid} (ts: {ts})")
                return ts
            else:
                logger.error(f"Failed to post initial live call Slack message: {data}")
        except Exception as e:
            logger.error(f"Exception posting initial live call Slack message: {e}")
    return None


async def update_live_call_slack_session(call_sid: str, caller_name: str, summary: str, tasks: list, crm_url: str = None):
    """
    Updates the existing live call Slack message using chat.update. Enforces rate-limiting.
    """
    session = active_slack_sessions.get(call_sid)
    if not session:
        return

    slack_token = os.getenv("SLACK_BOT_TOKEN")
    if not slack_token:
        return

    # Update session memory
    if summary:
        session["summary"] = summary
    if tasks:
        session["tasks"] = tasks
    if caller_name:
        session["caller_name"] = caller_name
    if crm_url:
        session["crm_url"] = crm_url

    now = time.time()
    # Debounce: max 1 update per 1.5 seconds to comply with Slack rate limits
    if now - session["last_update"] < 1.5:
        return
    session["last_update"] = now

    display_name = session.get("caller_name") or caller_name or "Caller"
    url = session.get("crm_url") or crm_url
    current_summary = session.get("summary") or summary
    current_tasks = session.get("tasks") or tasks

    sources = [{"type": "url", "url": url, "text": f"CiviCRM contact ({display_name})"}] if url else None

    blocks = build_during_call_blocks(call_sid, display_name, current_summary, current_tasks, sources=sources)

    async with httpx.AsyncClient(timeout=10.0) as client:
        headers = {"Authorization": f"Bearer {slack_token}"}
        try:
            res = await client.post(
                "https://slack.com/api/chat.update",
                headers=headers,
                json={
                    "channel": session["channel"],
                    "ts": session["ts"],
                    "blocks": blocks,
                    "text": f"Live call update for {display_name}"
                }
            )
            data = res.json()
            if not data.get("ok"):
                logger.warning(f"Slack chat.update returned error: {data}")
        except Exception as e:
            logger.error(f"Error updating live call Slack message: {e}")


async def finalize_live_call_slack_session(payload: dict, tasks: list = None):
    """
    Finalizes the live call message to the after-call layout and uploads the recording MP3 into the main channel.
    """
    call_sid = payload.get("CallSid", "unknown")
    caller = payload.get("From", "Caller")
    duration = payload.get("RecordingDuration")
    
    if duration and str(duration).isdigit():
        dur_int = int(duration)
        mins = dur_int // 60
        secs = dur_int % 60
        duration_str = f"{mins}min {secs}sec" if mins > 0 else f"{secs}sec"
    else:
        duration_str = ""

    summary_from_payload = payload.get("Summary", "")
    raw_recording_url = payload.get("RecordingUrl", "")

    session = active_slack_sessions.get(call_sid)
    slack_token = os.getenv("SLACK_BOT_TOKEN")
    channel_id = session["channel"] if session else (os.getenv("SLACK_CHANNEL_ID") or os.getenv("RECORDING_SLACK_CHANNEL_ID"))
    thread_ts = session["ts"] if session else None
    caller_name = (session.get("caller_name") if session else None) or caller
    crm_url = session.get("crm_url") if session else None
    
    summary = summary_from_payload or (session.get("summary") if session else "")
    final_tasks = tasks or (session.get("tasks") if session else []) or []

    sources = [{"type": "url", "url": crm_url, "text": f"CiviCRM contact ({caller_name})"}] if crm_url else None

    blocks = build_after_call_blocks(call_sid, caller_name, duration_str, summary, final_tasks, sources=sources)

    if slack_token and channel_id:
        async with httpx.AsyncClient(timeout=30.0) as client:
            headers = {"Authorization": f"Bearer {slack_token}"}

            # 1. Update live message to final state if session exists, else post new
            if thread_ts:
                try:
                    await client.post(
                        "https://slack.com/api/chat.update",
                        headers=headers,
                        json={
                            "channel": channel_id,
                            "ts": thread_ts,
                            "blocks": blocks,
                            "text": f"Call completed with {caller_name}"
                        }
                    )
                    logger.info(f"Finalized Slack message for call {call_sid}")
                except Exception as e:
                    logger.error(f"Error finalizing live Slack message: {e}")
            else:
                try:
                    msg_res = await client.post(
                        "https://slack.com/api/chat.postMessage",
                        headers=headers,
                        json={
                            "channel": channel_id,
                            "blocks": blocks,
                            "text": f"Call completed with {caller_name}"
                        }
                    )
                    msg_data = msg_res.json()
                    if msg_data.get("ok"):
                        thread_ts = msg_data.get("ts")
                except Exception as e:
                    logger.error(f"Error posting final Slack message: {e}")

            # 2. Download audio file from Twilio using 2-step redirect (Twilio API -> Amazon S3)
            audio_bytes = None
            if raw_recording_url:
                try:
                    account_sid = os.getenv("TWILIO_ACCOUNT_SID")
                    auth_token = os.getenv("TWILIO_AUTH_TOKEN")
                    clean_url = raw_recording_url
                    if "@" in clean_url:
                        clean_url = "https://" + clean_url.split("@", 1)[1]
                    if not clean_url.endswith(".mp3"):
                        clean_url += ".mp3"
                        
                    auth = (account_sid, auth_token) if (account_sid and auth_token) else None
                    
                    # Step 1: Request Twilio endpoint without auto-redirecting
                    rec_res = await client.get(clean_url, auth=auth, follow_redirects=False)
                    if rec_res.status_code in (301, 302, 303, 307):
                        redirect_url = rec_res.headers.get("Location")
                        # Step 2: Fetch MP3 content from S3 presigned URL WITHOUT Auth header
                        s3_res = await client.get(redirect_url, follow_redirects=True)
                        if s3_res.status_code == 200:
                            audio_bytes = s3_res.content
                        else:
                            logger.warning(f"S3 download failed with status {s3_res.status_code}")
                    elif rec_res.status_code == 200:
                        audio_bytes = rec_res.content
                    else:
                        logger.warning(f"Twilio recording download returned HTTP {rec_res.status_code}")
                except Exception as e:
                    logger.error(f"Error downloading Twilio audio: {e}")

            # 3. Upload audio file directly into main Slack channel
            if audio_bytes:
                try:
                    filename = f"call_recording_{call_sid}.mp3"
                    length = len(audio_bytes)
                    
                    get_url_res = await client.post(
                        "https://slack.com/api/files.getUploadURLExternal",
                        headers=headers,
                        data={
                            "filename": filename,
                            "length": str(length),
                            "alt_txt": f"Call Recording ({call_sid})"
                        }
                    )
                    get_url_data = get_url_res.json()
                    if get_url_data.get("ok"):
                        upload_url = get_url_data["upload_url"]
                        file_id = get_url_data["file_id"]
                        
                        upload_res = await client.post(
                            upload_url,
                            content=audio_bytes,
                            headers={"Content-Type": "audio/mpeg"}
                        )
                        if upload_res.status_code == 200:
                            complete_payload = {
                                "files": [{"id": file_id, "title": f"Recording - {caller_name}"}],
                                "channel_id": channel_id
                            }
                            # Omit thread_ts so audio file attaches directly into main channel feed
                            complete_res = await client.post(
                                "https://slack.com/api/files.completeUploadExternal",
                                headers=headers,
                                json=complete_payload
                            )
                            comp_data = complete_res.json()
                            if comp_data.get("ok"):
                                logger.info(f"Successfully uploaded audio recording to Slack channel for call {call_sid}")
                            else:
                                logger.error(f"files.completeUploadExternal failed: {comp_data}")
                except Exception as e:
                    logger.error(f"Error uploading audio file to Slack: {e}")

        active_slack_sessions.pop(call_sid, None)
        return

    # Fallback to Webhook URL
    webhook_url = os.getenv("RECORDING_SLACK_WEBHOOK_URL") or os.getenv("SLACK_WEBHOOK_URL")
    if webhook_url:
        async with httpx.AsyncClient(timeout=10.0) as client:
            try:
                await client.post(webhook_url, json={"blocks": blocks})
            except Exception as e:
                logger.error(f"Webhook fallback failed: {e}")
    active_slack_sessions.pop(call_sid, None)


def send_slack_knowledge_gap_notification(observation: str, logger_instance=None):
    """
    Dispatches a background task to post a knowledge base gap report to Slack.
    """
    log = logger_instance or logger
    webhook_url = os.getenv("SLACK_WEBHOOK_URL")
    if not webhook_url:
        log.error("SLACK_WEBHOOK_URL not found in environment")
        return False
    
    payload = {"message": f"Knowledge Base Gap Reported:\n{observation}"}
    asyncio.create_task(httpx.AsyncClient(timeout=4.5).post(webhook_url, json=payload))
    log.info(f"Notified Slack about missing knowledge: {observation[:50]}...")
    return True
