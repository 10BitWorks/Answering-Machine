import os
import asyncio
import httpx
from loguru import logger

def build_slack_call_blocks(payload: dict) -> list[dict]:
    """
    Constructs a rich Slack Block Kit layout for call summaries and transcripts.
    """
    call_sid = payload.get("CallSid", "N/A")
    caller = payload.get("From", "Unknown")
    recipient = payload.get("To", "Unknown")
    duration = payload.get("RecordingDuration")
    duration_str = f"{duration}s" if duration else "N/A"
    summary = payload.get("Summary", "No summary available.")
    transcript = payload.get("Transcript", "No transcript available.")
    recording_url = payload.get("RecordingUrl", "")

    # Format transcript cleanly with blockquotes for Slack markdown
    if transcript and transcript != "No transcript available.":
        formatted_lines = []
        for line in transcript.splitlines():
            line = line.strip()
            if line:
                formatted_lines.append(f"> {line}")
        formatted_transcript = "\n".join(formatted_lines)
        if len(formatted_transcript) > 2800:
            formatted_transcript = formatted_transcript[:2750] + "\n> ... *(transcript truncated)*"
    else:
        formatted_transcript = "_No transcript available._"

    blocks = [
        {
            "type": "header",
            "text": {
                "type": "plain_text",
                "text": "📞 Call Summary & Recording",
                "emoji": True
            }
        },
        {
            "type": "section",
            "fields": [
                {"type": "mrkdwn", "text": f"*Caller:* {caller}"},
                {"type": "mrkdwn", "text": f"*Duration:* {duration_str}"},
                {"type": "mrkdwn", "text": f"*To:* {recipient}"},
                {"type": "mrkdwn", "text": f"*Call SID:* `{call_sid}`"}
            ]
        },
        {"type": "divider"},
        {
            "type": "section",
            "text": {
                "type": "mrkdwn",
                "text": f"*📝 Summary:*\n{summary}"
            }
        },
        {"type": "divider"},
        {
            "type": "section",
            "text": {
                "type": "mrkdwn",
                "text": f"*💬 Transcript:*\n{formatted_transcript}"
            }
        }
    ]

    if recording_url:
        blocks.extend([
            {"type": "divider"},
            {
                "type": "section",
                "text": {
                    "type": "mrkdwn",
                    "text": f"🎵 *Recording Link:* <{recording_url}|Listen to Recording>"
                }
            }
        ])

    return blocks


async def post_call_results_to_slack(payload: dict):
    """
    Posts end-of-call results to Slack using Bot API Token & direct file upload,
    or falls back to Incoming Webhooks if Bot Token is not configured.
    """
    slack_token = os.getenv("SLACK_BOT_TOKEN")
    channel_id = os.getenv("SLACK_CHANNEL_ID") or os.getenv("RECORDING_SLACK_CHANNEL_ID")
    webhook_url = os.getenv("RECORDING_SLACK_WEBHOOK_URL") or os.getenv("SLACK_WEBHOOK_URL")

    blocks = build_slack_call_blocks(payload)
    call_sid = payload.get("CallSid", "unknown")
    raw_recording_url = payload.get("RecordingUrl", "")

    # Mode A: Slack Bot API Token + Channel ID (upload audio directly to Slack channel)
    if slack_token and channel_id:
        async with httpx.AsyncClient(timeout=30.0) as client:
            headers = {"Authorization": f"Bearer {slack_token}"}
            
            # 1. Post rich Block Kit message first
            thread_ts = None
            try:
                msg_res = await client.post(
                    "https://slack.com/api/chat.postMessage",
                    headers=headers,
                    json={
                        "channel": channel_id,
                        "blocks": blocks,
                        "text": f"New call recording & summary for {payload.get('From', 'Caller')}"
                    }
                )
                msg_data = msg_res.json()
                if msg_data.get("ok"):
                    thread_ts = msg_data.get("ts")
                else:
                    logger.error(f"Slack chat.postMessage failed: {msg_data}")
            except Exception as e:
                logger.error(f"Failed to post Block Kit message to Slack: {e}")

            # 2. Download audio file from Twilio if present
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
                    rec_res = await client.get(clean_url, auth=auth, follow_redirects=True)
                    if rec_res.status_code == 200:
                        audio_bytes = rec_res.content
                    else:
                        logger.warning(f"Failed to download Twilio recording: HTTP {rec_res.status_code}")
                except Exception as e:
                    logger.error(f"Error downloading Twilio audio recording: {e}")

            # 3. Upload audio file to Slack via files.getUploadURLExternal / files.completeUploadExternal
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
                                "files": [{"id": file_id, "title": f"Recording - {payload.get('From', 'Caller')}"}],
                                "channel_id": channel_id
                            }
                            if thread_ts:
                                complete_payload["thread_ts"] = thread_ts
                                
                            complete_res = await client.post(
                                "https://slack.com/api/files.completeUploadExternal",
                                headers=headers,
                                json=complete_payload
                            )
                            comp_data = complete_res.json()
                            if comp_data.get("ok"):
                                logger.info(f"Successfully uploaded call recording {call_sid} to Slack channel {channel_id}")
                            else:
                                logger.error(f"files.completeUploadExternal failed: {comp_data}")
                        else:
                            logger.error(f"Failed to POST audio bytes to Slack upload URL: status {upload_res.status_code}")
                    else:
                        logger.error(f"files.getUploadURLExternal failed: {get_url_data}")
                except Exception as e:
                    logger.error(f"Error uploading audio file to Slack: {e}")
        return

    # Mode B: Fallback to Webhook URL if Bot Token is not configured
    if webhook_url:
        async with httpx.AsyncClient(timeout=10.0) as client:
            try:
                resp = await client.post(webhook_url, json={"blocks": blocks})
                if resp.status_code != 200:
                    logger.warning(f"Slack webhook returned {resp.status_code} for Block Kit; falling back to raw payload")
                    await client.post(webhook_url, json=payload)
                else:
                    logger.info("Successfully posted call results to Slack webhook with Block Kit formatting")
            except Exception as e:
                logger.error(f"Failed to send recording webhook to Slack: {e}")


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
