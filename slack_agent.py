import os
import asyncio
import time
import math
import re
import json
import httpx
from loguru import logger

# Active Slack sessions store: {call_sid: {"ts": str, "channel": str, "last_update": float, "cnam_name": str, "caller_number": str, "caller_location": str, "crm_url": str, "summary": str, "tasks": list, "context": object, "speech_tracker": object}}
active_slack_sessions = {}

def format_us_phone(phone_str: str) -> str:
    """
    Formats E.164 or raw phone string (+18482180683) into US format: (848) 218-0683.
    """
    if not phone_str:
        return ""
    digits = re.sub(r"\D", "", phone_str)
    if len(digits) == 11 and digits.startswith("1"):
        digits = digits[1:]
    if len(digits) == 10:
        return f"({digits[:3]}) {digits[3:6]}-{digits[6:]}"
    return phone_str

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

async def fetch_twilio_call_cost(call_sid: str, duration_secs: int = 0) -> str:
    """Fetches or calculates actual call price from Twilio REST API."""
    account_sid = os.getenv("TWILIO_ACCOUNT_SID")
    auth_token = os.getenv("TWILIO_AUTH_TOKEN")
    
    if account_sid and auth_token and call_sid and call_sid != "unknown":
        url = f"https://api.twilio.com/2010-04-01/Accounts/{account_sid}/Calls/{call_sid}.json"
        try:
            async with httpx.AsyncClient(timeout=5.0) as client:
                res = await client.get(url, auth=(account_sid, auth_token))
                if res.status_code == 200:
                    data = res.json()
                    price_raw = data.get("price")
                    if price_raw is not None:
                        val = abs(float(price_raw))
                        return f"${val:.3f}" if val < 0.10 else f"${val:.4f}"
        except Exception as e:
            logger.error(f"Error fetching Twilio call cost for {call_sid}: {e}")

    # Fallback estimation based on call duration if API price is not finalized yet
    mins = max(1, math.ceil(duration_secs / 60)) if duration_secs > 0 else 1
    est_price = (mins * 0.0085) + 0.0050
    return f"${est_price:.3f}"

def build_card_block(cnam_name: str, caller_number: str = None, caller_location: str = None, call_sid: str = None) -> dict:
    """
    Builds Card block containing ONLY CNAM data (not CiviCRM data).
    Uses natural US phone formatting: (848) 218-0683.
    """
    title_text = cnam_name.strip().title() if (cnam_name and cnam_name.strip()) else "Caller"
    formatted_num = format_us_phone(caller_number) if caller_number else ""
    
    subtitle_parts = []
    if formatted_num:
        subtitle_parts.append(formatted_num)
    if caller_location and caller_location.strip():
        subtitle_parts.append(f"⬢ {caller_location.strip()}")
        
    subtitle_text = " ".join(subtitle_parts) if subtitle_parts else "Incoming Telephony Call"

    card = {
        "type": "card",
        "slack_icon": {
            "type": "icon",
            "name": "mobile"
        },
        "title": {
            "type": "mrkdwn",
            "text": title_text,
            "verbatim": False
        },
        "subtitle": {
            "type": "mrkdwn",
            "text": subtitle_text,
            "verbatim": False
        }
    }
    if call_sid:
        card["block_id"] = f"card_block_{call_sid}"
    return card

def build_during_call_blocks(call_sid: str, cnam_name: str, summary: str, tasks: list, sources: list = None, caller_number: str = None, caller_location: str = None) -> list[dict]:
    """
    Builds Slack Block Kit payload matching during-call layout with CNAM Card header.
    """
    card_block = build_card_block(cnam_name, caller_number=caller_number, caller_location=caller_location, call_sid=call_sid)

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
            "task_id": f"task_pending_{call_sid}",
            "title": "Listening for next response...",
            "status": "pending"
        })

    summary_text = summary if summary else f"Incoming call in progress from *{card_block['title']['text']}*..."
    
    blocks = [
        card_block,
        {
            "type": "context",
            "block_id": f"context_block_{call_sid}",
            "elements": [
                {
                    "type": "mrkdwn",
                    "text": summary_text
                }
            ]
        },
        {
            "type": "plan",
            "block_id": f"plan_block_{call_sid}",
            "plan_id": f"plan_{call_sid}",
            "title": f"Handling incoming call from {card_block['title']['text']}",
            "tasks": plan_tasks
        },
        {
            "type": "input",
            "block_id": f"input_block_{call_sid}",
            "dispatch_action": True,
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

def build_after_call_blocks(call_sid: str, cnam_name: str, duration_str: str, summary: str, tasks: list, sources: list = None, caller_number: str = None, caller_location: str = None, call_cost: str = "", ai_cost: str = "") -> list[dict]:
    """
    Builds Slack Block Kit payload matching after-call-example.json layout:
    1. Card block with title (CNAM name only), subtitle (formatted phone & location), and mobile icon
    2. Plan block with completed turn tasks
    3. Context block with summary text
    4. Container block (collapsible Call Details with API Cost table)
    """
    card_block = build_card_block(cnam_name, caller_number=caller_number, caller_location=caller_location, call_sid=call_sid)

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
            "task_id": f"task_1_{call_sid}",
            "title": "Call conversation completed",
            "status": "complete",
            "details": build_rich_text_object("Gracefully ended call")
        })

    display_name = card_block["title"]["text"]
    summary_text = summary if summary else f"Call completed with *{display_name}*."
    title_text = f"{duration_str} call from {display_name}" if duration_str else f"Call from {display_name}"

    plan_block = {
        "type": "plan",
        "block_id": f"plan_block_{call_sid}",
        "plan_id": f"plan_{call_sid}",
        "title": title_text,
        "tasks": plan_tasks
    }

    context_block = {
        "type": "context",
        "block_id": f"context_block_{call_sid}",
        "elements": [
            {
                "type": "mrkdwn",
                "text": summary_text
            }
        ]
    }


    try:
        c_val = float(call_cost.replace("$", "")) if "$" in call_cost else 0.000
        a_val = float(ai_cost.replace("$", "")) if "$" in ai_cost else 0.000
        total_val = c_val + 0.0100 + a_val
        total_cost_str = f"${total_val:.4f}"
    except Exception:
        total_cost_str = "$0.00"

    details_container = {
        "type": "container",
        "block_id": "bkb_container_collapsible",
        "title": {
            "type": "plain_text",
            "text": "Call Details"
        },
        "is_collapsible": True,
        "default_collapsed": True,
        "child_blocks": [
            {
                "type": "header",
                "text": {
                    "type": "plain_text",
                    "text": "API Cost",
                    "emoji": True
                }
            },
            {
                "type": "table",
                "rows": [
                    [
                        build_rich_text_object("Call"),
                        build_rich_text_object("CNAM (Caller ID)"),
                        build_rich_text_object("AI"),
                        build_rich_text_object("Total")
                    ],
                    [
                        build_rich_text_object(call_cost),
                        build_rich_text_object("$0.01"),
                        build_rich_text_object(ai_cost),
                        build_rich_text_object(total_cost_str)
                    ]
                ]
            }
        ]
    }

    blocks = [card_block, plan_block, context_block, details_container]
    return blocks


async def start_live_call_slack_session(call_sid: str, cnam_name: str, caller_number: str, crm_url: str = None, context=None, speech_tracker=None, caller_location: str = None) -> str:
    """
    Posts the initial 'during call' Slack Block Kit message when a call connects.
    cnam_name MUST contain CNAM name only (never CiviCRM info).
    """
    slack_token = os.getenv("SLACK_BOT_TOKEN")
    channel_id = os.getenv("SLACK_CHANNEL_ID") or os.getenv("RECORDING_SLACK_CHANNEL_ID")
    if not (slack_token and channel_id):
        logger.info("SLACK_BOT_TOKEN or SLACK_CHANNEL_ID not set; skipping live Slack session.")
        return None

    sources = [{"type": "url", "url": crm_url, "text": f"CiviCRM contact ({cnam_name})"}] if crm_url else None
    blocks = build_during_call_blocks(call_sid, cnam_name, "", [], sources=sources, caller_number=caller_number, caller_location=caller_location)

    async with httpx.AsyncClient(timeout=10.0) as client:
        headers = {"Authorization": f"Bearer {slack_token}"}
        try:
            res = await client.post(
                "https://slack.com/api/chat.postMessage",
                headers=headers,
                json={
                    "channel": channel_id,
                    "blocks": blocks,
                    "text": f"Handling incoming call from {cnam_name or caller_number}"
                }
            )
            data = res.json()
            if data.get("ok"):
                ts = data.get("ts")
                active_slack_sessions[call_sid] = {
                    "ts": ts,
                    "channel": channel_id,
                    "last_update": time.time(),
                    "cnam_name": cnam_name,
                    "caller_number": caller_number,
                    "caller_location": caller_location,
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


async def update_live_call_slack_session(call_sid: str, cnam_name: str, summary: str, tasks: list, crm_url: str = None, caller_number: str = None, caller_location: str = None):
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
    if cnam_name:
        session["cnam_name"] = cnam_name
    if caller_number:
        session["caller_number"] = caller_number
    if caller_location:
        session["caller_location"] = caller_location
    if crm_url:
        session["crm_url"] = crm_url

    now = time.time()
    # Debounce: max 1 update per 1.5 seconds to comply with Slack rate limits
    if now - session["last_update"] < 1.5:
        return
    session["last_update"] = now

    cnam = session.get("cnam_name") or cnam_name or "Caller"
    c_number = session.get("caller_number") or caller_number
    c_loc = session.get("caller_location") or caller_location
    url = session.get("crm_url") or crm_url
    current_summary = session.get("summary") or summary
    current_tasks = session.get("tasks") or tasks

    sources = [{"type": "url", "url": url, "text": f"CiviCRM contact ({cnam})"}] if url else None

    blocks = build_during_call_blocks(call_sid, cnam, current_summary, current_tasks, sources=sources, caller_number=c_number, caller_location=c_loc)

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
                    "text": f"Live call update for {cnam}"
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
    
    dur_secs = 0
    if duration and str(duration).isdigit():
        dur_secs = int(duration)
        mins = dur_secs // 60
        secs = dur_secs % 60
        duration_str = f"{mins}min {secs}sec" if mins > 0 else f"{secs}sec"
    else:
        duration_str = ""

    summary_from_payload = payload.get("Summary", "")
    raw_recording_url = payload.get("RecordingUrl", "")

    session = active_slack_sessions.get(call_sid)
    slack_token = os.getenv("SLACK_BOT_TOKEN")
    channel_id = session["channel"] if session else (os.getenv("SLACK_CHANNEL_ID") or os.getenv("RECORDING_SLACK_CHANNEL_ID"))
    thread_ts = session["ts"] if session else None
    
    cnam_name = (session.get("cnam_name") if session else None) or payload.get("CallerName", "")
    crm_url = session.get("crm_url") if session else None
    
    summary = summary_from_payload or (session.get("summary") if session else "")
    final_tasks = tasks or (session.get("tasks") if session else []) or []

    caller_number = (session.get("caller_number") if session else None) or caller
    caller_city = payload.get("CallerCity", "")
    caller_state = payload.get("CallerState", "")
    caller_zip = payload.get("CallerZip", "")
    loc_parts = [p for p in [caller_city, f"{caller_state} {caller_zip}".strip()] if p]
    caller_location = ", ".join(loc_parts) if loc_parts else (session.get("caller_location") if session else None)

    # Fetch actual call cost from Twilio REST API
    call_cost = await fetch_twilio_call_cost(call_sid, duration_secs=dur_secs)

    sources = [{"type": "url", "url": crm_url, "text": f"CiviCRM contact ({cnam_name})"}] if crm_url else None

    blocks = build_after_call_blocks(
        call_sid,
        cnam_name,
        duration_str,
        summary,
        final_tasks,
        sources=sources,
        caller_number=caller_number,
        caller_location=caller_location,
        call_cost=call_cost
    )

    loc_str = f" · ⬢ {caller_location}" if caller_location else ""
    num_str = f" · {format_us_phone(caller_number)}" if caller_number else ""
    initial_comment_text = f"*{cnam_name or caller}*{num_str}{loc_str}\n{duration_str} · {call_cost}\n\n{summary or 'Call completed.'}"
    completed_tasks = [t for t in final_tasks if t.get("status") == "complete"]
    if completed_tasks:
        task_lines = "\n".join(f"✓ {t.get('title', '')}" for t in completed_tasks[:5])
        initial_comment_text += f"\n\n{task_lines}"

    if slack_token and channel_id:
        async with httpx.AsyncClient(timeout=30.0) as client:
            headers = {"Authorization": f"Bearer {slack_token}"}

            # 1. Download audio file from Twilio
            audio_bytes = None
            if raw_recording_url:
                logger.info(f"Downloading Twilio recording for call {call_sid} from URL: {raw_recording_url}")
                try:
                    account_sid = os.getenv("TWILIO_ACCOUNT_SID")
                    auth_token = os.getenv("TWILIO_AUTH_TOKEN")
                    clean_url = raw_recording_url
                    if "@" in clean_url:
                        clean_url = "https://" + clean_url.split("@", 1)[1]
                    if not clean_url.endswith(".mp3"):
                        clean_url += ".mp3"
                        
                    auth = (account_sid, auth_token) if (account_sid and auth_token) else None
                    rec_res = await client.get(clean_url, auth=auth, follow_redirects=False)
                    if rec_res.status_code in (301, 302, 303, 307, 308):
                        redirect_url = rec_res.headers.get("Location") or rec_res.headers.get("location")
                        if redirect_url:
                            # Use clean unauthenticated client for S3 presigned URL to prevent Basic Auth header collisions
                            async with httpx.AsyncClient(timeout=30.0) as s3_client:
                                s3_res = await s3_client.get(redirect_url, follow_redirects=True)
                                if s3_res.status_code == 200:
                                    audio_bytes = s3_res.content
                                    logger.info(f"Successfully downloaded audio bytes ({len(audio_bytes)} bytes) from S3 for call {call_sid}")
                                else:
                                    logger.error(f"S3 download failed for call {call_sid} with status {s3_res.status_code}: {s3_res.text[:200]}")
                        else:
                            logger.error(f"Twilio recording redirect missing Location header for call {call_sid}")
                    elif rec_res.status_code == 200:
                        audio_bytes = rec_res.content
                        logger.info(f"Successfully downloaded audio bytes ({len(audio_bytes)} bytes) directly for call {call_sid}")
                    else:
                        logger.error(f"Twilio recording download returned HTTP {rec_res.status_code} for call {call_sid}: {rec_res.text[:200]}")
                except Exception as e:
                    logger.error(f"Error downloading Twilio audio for call {call_sid}: {e}")
            else:
                logger.warning(f"No RecordingUrl provided in callback payload for call {call_sid}; skipping audio attachment.")

            # 2. Upload audio file and create file-share message in Slack channel
            recording_uploaded = False
            if audio_bytes:
                try:
                    filename = f"call_recording_{call_sid}.mp3"
                    length = len(audio_bytes)
                    
                    get_url_res = await client.post(
                        "https://slack.com/api/files.getUploadURLExternal",
                        headers=headers,
                        params={
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
                            # CRITICAL FIX: Send completeUploadExternal as form-encoded data with files as JSON string
                            complete_res = await client.post(
                                "https://slack.com/api/files.completeUploadExternal",
                                headers=headers,
                                data={
                                    "files": json.dumps([{"id": file_id, "title": f"Recording - {cnam_name or caller}"}]),
                                    "channel_id": channel_id,
                                    "initial_comment": initial_comment_text
                                }
                            )
                            comp_data = complete_res.json()
                            logger.info(f"completeUploadExternal response for call {call_sid}: {json.dumps(comp_data)[:1000]}")
                            
                            if comp_data.get("ok"):
                                logger.info(f"Successfully completed Slack audio file upload for call {call_sid}")
                                
                                # Extract file_share_ts directly from response or by polling files.info
                                file_share_ts = None
                                files_arr = comp_data.get("files") or []
                                if files_arr:
                                    file_obj = files_arr[0]
                                    shares = file_obj.get("shares", {})
                                    for share_type in ("public", "private"):
                                        chans = shares.get(share_type, {})
                                        if channel_id in chans and chans[channel_id]:
                                            file_share_ts = chans[channel_id][0].get("ts")
                                            break

                                # Polling files.info fallback: Slack async post processing may take a short moment to publish shares
                                if not file_share_ts and file_id:
                                    for attempt in range(4):
                                        await asyncio.sleep(0.5)
                                        try:
                                            info_res = await client.post(
                                                "https://slack.com/api/files.info",
                                                headers=headers,
                                                data={"file": file_id}
                                            )
                                            info_data = info_res.json()
                                            if info_data.get("ok"):
                                                shares = info_data.get("file", {}).get("shares", {})
                                                for share_type in ("public", "private"):
                                                    chans = shares.get(share_type, {})
                                                    if channel_id in chans and chans[channel_id]:
                                                        file_share_ts = chans[channel_id][0].get("ts")
                                                        break
                                                if file_share_ts:
                                                    logger.info(f"files.info polling attempt {attempt+1} retrieved file_share_ts {file_share_ts} for call {call_sid}")
                                                    break
                                            else:
                                                logger.warning(f"files.info attempt {attempt+1} returned error: {info_data.get('error')}")
                                        except Exception as e:
                                            logger.error(f"files.info polling attempt {attempt+1} failed for call {call_sid}: {e}")

                                if file_share_ts:
                                    logger.info(f"Extracted file_share_ts {file_share_ts} for call {call_sid}. Attempting chat.update with Block Kit blocks...")
                                    try:
                                        update_res = await client.post(
                                            "https://slack.com/api/chat.update",
                                            headers=headers,
                                            json={
                                                "channel": channel_id,
                                                "ts": file_share_ts,
                                                "blocks": blocks,
                                                "text": f"Call completed with {cnam_name or caller}"
                                            }
                                        )
                                        update_data = update_res.json()
                                        if update_data.get("ok"):
                                            logger.info(f"Successfully updated file-share message {file_share_ts} with Block Kit blocks for call {call_sid}")
                                        else:
                                            logger.warning(f"chat.update on file-share message failed for call {call_sid}: {update_data}; preserving initial_comment text.")
                                    except Exception as e:
                                        logger.error(f"Error updating file-share message with blocks for call {call_sid}: {e}")

                                    # Delete old live tracking message so ONLY the single audio file message remains in the channel
                                    if thread_ts and thread_ts != file_share_ts:
                                        try:
                                            del_res = await client.post(
                                                "https://slack.com/api/chat.delete",
                                                headers=headers,
                                                data={"channel": channel_id, "ts": thread_ts}
                                            )
                                            logger.info(f"Deleted live tracking message {thread_ts} for call {call_sid}: {del_res.json()}")
                                        except Exception as del_err:
                                            logger.error(f"Error deleting live message {thread_ts}: {del_err}")

                                    recording_uploaded = True
                                else:
                                    logger.warning(f"Could not extract file_share_ts for call {call_sid}; preserving live tracking message as fallback.")
                            else:
                                logger.error(f"files.completeUploadExternal failed for call {call_sid}: {comp_data}")
                        else:
                            logger.error(f"Upload to Slack binary endpoint failed for call {call_sid} (HTTP {upload_res.status_code}): {upload_res.text[:200]}")
                    else:
                        logger.error(f"files.getUploadURLExternal failed for call {call_sid}: {get_url_data}")
                except Exception as e:
                    logger.error(f"Error uploading audio file to Slack for call {call_sid}: {e}")

            # Fallback path if audio recording download/upload failed: finalize live tracking message or post new message
            if not recording_uploaded:
                if thread_ts:
                    try:
                        await client.post(
                            "https://slack.com/api/chat.update",
                            headers=headers,
                            json={
                                "channel": channel_id,
                                "ts": thread_ts,
                                "blocks": blocks,
                                "text": f"Call completed with {cnam_name or caller}"
                            }
                        )
                        logger.info(f"Finalized live Slack message for call {call_sid} (fallback path without audio attachment)")
                    except Exception as e:
                        logger.error(f"Error finalizing live Slack message in fallback: {e}")
                else:
                    try:
                        await client.post(
                            "https://slack.com/api/chat.postMessage",
                            headers=headers,
                            json={
                                "channel": channel_id,
                                "blocks": blocks,
                                "text": f"Call completed with {cnam_name or caller}"
                            }
                        )
                        logger.info(f"Posted new final Slack message for call {call_sid} (fallback path without audio attachment)")
                    except Exception as e:
                        logger.error(f"Error posting final Slack message in fallback: {e}")

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
