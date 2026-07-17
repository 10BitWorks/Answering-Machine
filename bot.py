import os
import sys
from datetime import datetime
from zoneinfo import ZoneInfo
from pathlib import Path
import asyncio
import random
import httpx
from fastapi import FastAPI, WebSocket, Request
from fastapi.responses import HTMLResponse, Response
from loguru import logger
logger.remove()
logger.add(sys.stderr, level="INFO")
from dotenv import load_dotenv
import uvloop
uvloop.install()

# Ensure logs directory exists
os.makedirs("logs", exist_ok=True)

from pipecat.adapters.schemas.function_schema import FunctionSchema
from pipecat.adapters.schemas.tools_schema import AdapterType, ToolsSchema
from pipecat.frames.frames import LLMRunFrame, EndFrame, CancelTaskFrame, EndTaskFrame, BotStartedSpeakingFrame, BotStoppedSpeakingFrame, Frame, TranscriptionFrame, FunctionCallResultProperties, TextFrame, AudioRawFrame, LLMContextFrame, LLMMessagesAppendFrame
from pipecat.pipeline.pipeline import Pipeline
from pipecat.pipeline.runner import PipelineRunner
from pipecat.pipeline.task import PipelineParams, PipelineTask
from pipecat.processors.aggregators.llm_context import LLMContext
from pipecat.services.google.gemini_live.llm import GeminiLiveLLMService, GeminiVADParams
from pipecat.processors.aggregators.llm_response_universal import LLMContextAggregatorPair
from pipecat.processors.frame_processor import FrameDirection, FrameProcessor
from pipecat.services.llm_service import FunctionCallParams
from pipecat.runner.utils import parse_telephony_websocket
from pipecat.services.google.gemini_live.llm import GeminiLiveLLMService
from pipecat.transports.websocket.fastapi import FastAPIWebsocketTransport, FastAPIWebsocketParams
from pipecat.serializers.twilio import TwilioFrameSerializer

load_dotenv(override=True)

import sync_knowledgebase
import civicrm_lookup
import civicrm_agent
import zammad_cti
import zammad_agent

app = FastAPI()

# Configuration
GOOGLE_API_KEY = os.getenv("GOOGLE_API_KEY")
# Note: Using the model name without the "models/" prefix or v1alpha features
# was required to resolve the 1011 internal error.
MODEL_NAME = "gemini-3.1-flash-live-preview"

if not GOOGLE_API_KEY:
    logger.error("GOOGLE_API_KEY not found in environment variables")
    sys.exit(1)

STUDIO_WEBHOOK_URL = os.getenv("STUDIO_WEBHOOK_URL")
if not STUDIO_WEBHOOK_URL:
    logger.warning("STUDIO_WEBHOOK_URL not found in environment. Call continuation will be disabled.")

# Global state for call transfers and hangups
pending_transfers = {}
pending_hangups = set()
active_calls = {} # {call_sid: {"from": ..., "to": ...}}
call_transcripts = {} # {call_sid: "transcript text"}
goclaw_sessions = {} # {call_sid: [{"role": "user", "content": "..."}]}

# Sync knowledgebase on startup
logger.info("Syncing knowledgebase from Zammad...")
try:
    sync_knowledgebase.main()
except Exception as e:
    logger.error(f"Failed to sync knowledgebase: {e}")

# Load system prompt from file and append knowledgebase
SYSTEM_PROMPT_PATH = Path(__file__).parent / "SYSTEM_PROMPT.md"
KNOWLEDGEBASE_DIR = Path(__file__).parent / "knowledgebase"

try:
    with open(SYSTEM_PROMPT_PATH, "r") as f:
        SYSTEM_PROMPT = f.read()
    
    if KNOWLEDGEBASE_DIR.exists():
        kb_content = "\n\n# KNOWLEDGE BASE\n"
        for md_file in KNOWLEDGEBASE_DIR.glob("**/*.md"):
            with open(md_file, "r") as f:
                kb_content += f"\n\n## {md_file.name}\n"
                kb_content += f.read()
        SYSTEM_PROMPT += kb_content
        
    logger.info(f"Loaded system prompt and knowledgebase. Total length: {len(SYSTEM_PROMPT)}")
except Exception as e:
    logger.error(f"Failed to load system prompt or knowledgebase: {e}")
    sys.exit(1)

@app.api_route("/twiml", methods=["GET", "POST"])
async def twiml(request: Request):
    host = request.url.netloc
    
    if request.method == "POST":
        data = await request.form()
    else:
        data = request.query_params
        
    from_number = data.get("From", "Unknown Caller")
    to_number = data.get("To", "Unknown")
    caller_name = data.get("CallerName", "")
        
    twiml_response = f"""<?xml version="1.0" encoding="UTF-8"?>
    <Response>
        <Start>
            <Recording channels="dual" recordingStatusCallback="https://{host}/recording-callback" recordingStatusCallbackMethod="POST" />
        </Start>
        <Connect>
            <Stream url="wss://{host}/ws">
                <Parameter name="caller_number" value="{from_number}" />
                <Parameter name="destination_number" value="{to_number}" />
                <Parameter name="caller_name" value="{caller_name}" />
            </Stream>
        </Connect>
        <Redirect method="POST">https://{host}/post_bot</Redirect>
    </Response>"""
    return Response(content=twiml_response, media_type="application/xml")

@app.post("/post_bot")
async def post_bot(request: Request):
    form_data = await request.form()
    call_sid = form_data.get("CallSid")
    
    transfer_data = pending_transfers.pop(call_sid, None)
    if transfer_data:
        target_number = transfer_data.get("number")
        target_name = transfer_data.get("name")
        
        # Get caller info to send the 'answer' event to Zammad
        call_info = active_calls.get(call_sid, {})
        from_num = call_info.get("from", "Unknown")
        
        logger.info(f"Executing transfer for {call_sid} to {target_name} ({target_number})")
        
        # Clean up any lingering hangup request just in case
        if call_sid in pending_hangups:
            pending_hangups.remove(call_sid)
            
        # Push 'answer' to Zammad with the new target person's name
        asyncio.create_task(zammad_cti.push_cti_event(
            event="answer",
            from_number=from_num,
            to_number=target_number,
            direction="in",
            call_id=call_sid,
            user_name=target_name
        ))
        
        twiml_response = f"""<?xml version="1.0" encoding="UTF-8"?>
        <Response>
            <Dial>{target_number}</Dial>
        </Response>"""
        return Response(content=twiml_response, media_type="application/xml")
        
    if call_sid in pending_hangups:
        logger.info(f"Executing hangup for {call_sid}")
        pending_hangups.remove(call_sid)
        return Response(content='<Response><Hangup/></Response>', media_type="application/xml")
    
    # Cleanup active_calls
    active_calls.pop(call_sid, None)
    goclaw_sessions.pop(call_sid, None)
    
    if STUDIO_WEBHOOK_URL:
        sep = "&" if "?" in STUDIO_WEBHOOK_URL else "?"
        studio_return_url = f"{STUDIO_WEBHOOK_URL}{sep}FlowEvent=return"
        logger.info(f"Returning call {call_sid} to Studio Flow")
        twiml = f"""<?xml version="1.0" encoding="UTF-8"?>
        <Response>
            <Redirect method="POST">{studio_return_url}</Redirect>
        </Response>"""
        return Response(content=twiml, media_type="application/xml")
    
    return Response(content='<Response><Hangup/></Response>', media_type="application/xml")

@app.post("/recording-callback")
async def recording_callback(request: Request):
    form_data = await request.form()
    payload = {k: v for k, v in form_data.items()}
    call_sid = payload.get("CallSid")
    
    if call_sid in call_transcripts:
        payload["Transcript"] = call_transcripts.pop(call_sid)
    else:
        payload["Transcript"] = "No transcript available."
        
    # Inject HTTP basic auth into the RecordingUrl if present
    if "RecordingUrl" in payload:
        account_sid = os.getenv("TWILIO_ACCOUNT_SID")
        auth_token = os.getenv("TWILIO_AUTH_TOKEN")
        if account_sid and auth_token:
            url = payload["RecordingUrl"]
            if url.startswith("https://"):
                payload["RecordingUrl"] = f"https://{account_sid}:{auth_token}@{url[8:]}"
                
    recording_webhook = os.getenv("RECORDING_SLACK_WEBHOOK_URL")
    if recording_webhook:
        async with httpx.AsyncClient() as client:
            try:
                await client.post(recording_webhook, json=payload)
            except Exception as e:
                logger.error(f"Failed to send recording webhook: {e}")
                
    return Response(status_code=204)


@app.websocket("/ws")
async def websocket_endpoint(websocket: WebSocket):
    await websocket.accept()
    logger.info("Twilio WebSocket connection accepted")
    
    call_history = []

    transport_type, call_data = await parse_telephony_websocket(websocket)
    call_sid = call_data["call_id"]
    # Choose a random voice for this call
    VOICES = [
        "Zephyr", "Puck", "Charon", "Kore", "Fenrir", "Leda", "Orus", "Aoede",
        "Callirrhoe", "Autonoe", "Enceladus", "Iapetus", "Umbriel", "Algieba",
        "Despina", "Erinome", "Algenib", "Rasalgethi", "Laomedeia", "Achernar",
        "Alnilam", "Schedar", "Gacrux", "Pulcherrima", "Achird", "Zubenelgenubi",
        "Vindemiatrix", "Sadachbia", "Sadaltager", "Sulafat"
    ]
    selected_voice = random.choice(VOICES)
    
    # Set up per-call logging
    Path("logs").mkdir(exist_ok=True)
    log_file = f"logs/call_{call_sid}.log"
    handler_id = logger.add(
        log_file, 
        filter=lambda record: record["extra"].get("call_id") == call_sid,
        format="{time} | {level: <8} | {message}",
        level="DEBUG" # Keep debug on for call files to catch jitter
    )
    call_logger = logger.bind(call_id=call_sid)
    call_logger.info(f"Accepted {transport_type} call: {call_data} (Voice: {selected_voice})")
    
    caller_number = call_data.get("body", {}).get("caller_number", "Unknown Caller")
    destination_number = call_data.get("body", {}).get("destination_number", "Unknown")
    caller_name = call_data.get("body", {}).get("caller_name", "")
    # Title-case CNAM (arrives as ALL CAPS like "DAVID BLUM") and extract first name
    if caller_name:
        caller_name = caller_name.strip().title()
        caller_first_name = caller_name.split()[0] if caller_name.split() else caller_name
    else:
        caller_first_name = ""

    # Store active call info for CTI updates during transfer
    active_calls[call_sid] = {
        "from": caller_number,
        "to": destination_number
    }

    serializer = TwilioFrameSerializer(
        stream_sid=call_data["stream_id"],
        call_sid=call_sid,
        account_sid=os.getenv("TWILIO_ACCOUNT_SID"),
        auth_token=os.getenv("TWILIO_AUTH_TOKEN"),
        params=TwilioFrameSerializer.InputParams(
            auto_hang_up=False
        )
    )

    transport = FastAPIWebsocketTransport(
        websocket=websocket,
        params=FastAPIWebsocketParams(
            audio_in_enabled=True,
            audio_out_enabled=True,
            add_wav_header=False,
            serializer=serializer,
            fixed_audio_packet_size=320,
            audio_out_can_send_silence=False
        )
    )

    tools = ToolsSchema(
        standard_tools=[
            FunctionSchema(
                name="end_call",
                description="Ends the phone call. CRITICAL: You MUST call this tool immediately in the exact same turn after you say goodbye or conclude the conversation. Do not wait for the user to hang up first.",
                properties={},
                required=[]
            ),
            FunctionSchema(
                name="report_missing_knowledge",
                description="Report a gap in the knowledge base. Use this silently when you cannot find a specific answer or when the existing docs are insufficient. Describe who is calling, what they specifically want to know, and why the current documentation didn't help.",
                properties={
                    "observation": {
                        "type": "string",
                        "description": "Detailed description of the knowledge gap, including caller context and why the documentation was insufficient."
                    }
                },
                required=["observation"]
            ),
            FunctionSchema(
                name="transfer_call",
                description="Transfer the current call to another phone number. Use this when the user asks to speak to a person or a specific volunteer.",
                properties={
                    "phone_number": {
                        "type": "string",
                        "description": "The E.164 formatted phone number to transfer to (e.g. +12105551212)."
                    },
                    "contact_name": {
                        "type": "string",
                        "description": "The name of the person being transferred to (e.g. Greg)."
                    }
                },
                required=["phone_number", "contact_name"]
            ),
            FunctionSchema(
                name="lookup_contact",
                description="Look up a contact name in the CiviCRM database to get their phone number. This does NOT transfer the call. You must use transfer_call with the resulting phone number to actually transfer.",
                properties={
                    "contact_name": {
                        "type": "string",
                        "description": "The full name of the contact to look up (e.g. Greg Thibodeaux)."
                    }
                },
                required=["contact_name"]
            ),
            FunctionSchema(
                name="check_my_membership",
                description="Checks the caller's current membership status, type, and expiration date. Only use this if the caller is recognized.",
                properties={},
                required=[]
            ),
            FunctionSchema(
                name="list_my_contact_info",
                description="Lists all addresses, phone numbers, and email addresses we have on file for the caller. Only use this if the caller is recognized.",
                properties={},
                required=[]
            ),
            FunctionSchema(
                name="create_my_contact_record",
                description="Creates a new contact record for the current caller in our database. Use this IMMEDIATELY after an unrecognized caller has provided their first and last name, so that we can accurately log their inquiry.",
                properties={
                    "first_name": {"type": "string", "description": "The caller's first name."},
                    "last_name": {"type": "string", "description": "The caller's last name."}
                },
                required=["first_name", "last_name"]
            ),
            FunctionSchema(
                name="add_new_address",
                description="Adds a new address to the caller's record. Does NOT delete old ones.",
                properties={
                    "street_address": {"type": "string", "description": "Street address (e.g. 123 Main St)."},
                    "city": {"type": "string", "description": "City (e.g. San Antonio)."},
                    "postal_code": {"type": "string", "description": "5-digit zip code (e.g. 78201)."},
                    "is_primary": {"type": "boolean", "description": "Whether this should be the primary address."}
                },
                required=["street_address", "city", "postal_code"]
            ),
            FunctionSchema(
                name="add_new_phone",
                description="Adds a new phone number to the caller's record.",
                properties={
                    "phone_number": {"type": "string", "description": "Phone number in E.164 format (e.g. +12105551212)."},
                    "is_primary": {"type": "boolean", "description": "Whether this should be the primary phone number."}
                },
                required=["phone_number"]
            ),
            FunctionSchema(
                name="add_new_email",
                description="Adds a new email address to the caller's record.",
                properties={
                    "email_address": {"type": "string", "description": "Email address (e.g. user@example.com)."},
                    "is_primary": {"type": "boolean", "description": "Whether this should be the primary email."}
                },
                required=["email_address"]
            ),
            FunctionSchema(
                name="set_info_as_primary",
                description="Changes which existing record is marked as primary. Requires the Record ID (provided by list_my_contact_info) and the entity type.",
                properties={
                    "entity_type": {
                        "type": "string",
                        "enum": ["Address", "Phone", "Email"],
                        "description": "The type of record being updated."
                    },
                    "record_id": {
                        "type": "integer",
                        "description": "The unique ID of the specific record to make primary."
                    }
                },
                required=["entity_type", "record_id"]
            ),
            FunctionSchema(
                name="ask_support_bot",
                description="Consult the 10BitWorks support bot for questions you cannot answer from your own knowledge. Call this tool IMMEDIATELY when you don't know the answer. The tool will automatically return a 'processing' instruction telling you to stall naturally while it fetches the answer in the background.",
                properties={
                    "question": {
                        "type": "string",
                        "description": "The caller's question, rephrased clearly for the support bot. Include any relevant context like whether they are a member or prospective member."
                    }
                },
                required=["question"]
            )
        ],
        custom_tools={AdapterType.GEMINI: [{"google_search": {}}]},
    )

    llm = GeminiLiveLLMService(
        api_key=GOOGLE_API_KEY,
        settings=GeminiLiveLLMService.Settings(
            model=MODEL_NAME,
            system_instruction=SYSTEM_PROMPT,
            voice=selected_voice,
            vad=GeminiVADParams(
                start_sensitivity="START_SENSITIVITY_LOW"
            )
        ),
        tools=tools,
        reconnect_on_error=False
    )

    class SpeechTracker(FrameProcessor):
        def __init__(self):
            super().__init__()
            self.is_speaking = False

        async def process_frame(self, frame: Frame, direction: FrameDirection):
            await super().process_frame(frame, direction)
            if isinstance(frame, BotStartedSpeakingFrame):
                self.is_speaking = True
            elif isinstance(frame, BotStoppedSpeakingFrame):
                self.is_speaking = False
            elif isinstance(frame, TranscriptionFrame):
                if frame.user_id == "user":
                    call_history.append(f"[User] {frame.text}")
            elif isinstance(frame, LLMContextFrame):
                for msg in reversed(frame.context.messages):
                    if msg.get("role") == "assistant" and msg.get("content"):
                        call_history.append(f"[Bot] {msg['content']}")
                        break
            await self.push_frame(frame, direction)

    speech_tracker = SpeechTracker()
    is_terminating = False # Prevent double-termination race conditions
    
    async def await_bot_silence(timeout=4.0):
        """Waits for the bot to stop speaking, with a timeout to avoid pipeline hangs."""
        start = asyncio.get_event_loop().time()
        while speech_tracker.is_speaking and (asyncio.get_event_loop().time() - start) < timeout:
            await asyncio.sleep(0.1)

    async def wait_and_terminate():
        call_logger.info("wait_and_terminate started: monitoring for final speech.")
        
        # 1. Wait for bot to START speaking (the "Goodbye" turn)
        # We look for the transition into speaking state.
        start_wait = asyncio.get_event_loop().time()
        max_start_wait = 5.0 # Max time to wait for the LLM to start its final turn
        
        while not speech_tracker.is_speaking and (asyncio.get_event_loop().time() - start_wait) < max_start_wait:
            await asyncio.sleep(0.05)
            
        if speech_tracker.is_speaking:
            call_logger.info("Final speech detected. Waiting for bot to finish...")
            # 2. Wait for bot to FINISH speaking
            max_speech_wait = 20.0
            speech_start = asyncio.get_event_loop().time()
            while speech_tracker.is_speaking and (asyncio.get_event_loop().time() - speech_start) < max_speech_wait:
                await asyncio.sleep(0.05)
            call_logger.info("Final speech finished.")
        else:
            call_logger.warning("Timed out waiting for final speech to start. Terminating anyway.")

        # 3. Small flush window (0.2s) to ensure final packet hits the OS buffer
        await asyncio.sleep(0.2)
        
        call_logger.info("Terminating pipeline with CancelTaskFrame.")
        await llm.push_frame(CancelTaskFrame(), FrameDirection.UPSTREAM)

    async def hang_up(params: FunctionCallParams):
        nonlocal is_terminating
        call_sid = call_data["call_id"]
        call_logger.info(f"Bot is ending the call {call_sid} via end_call tool")
        pending_hangups.add(call_sid)
        if not is_terminating:
            is_terminating = True
            asyncio.create_task(wait_and_terminate())
        # Return success immediately so the bot can say its final goodbye turn
        return {"status": "hangup_initiated", "instruction": "You are now hanging up. Say a brief and polite goodbye to the user now before the connection is severed."}

    async def notify_slack(params: FunctionCallParams):
        observation = params.arguments.get("observation")
        webhook_url = os.getenv("SLACK_WEBHOOK_URL")
        if not webhook_url:
            call_logger.error("SLACK_WEBHOOK_URL not found in environment")
            await params.result_callback({"status": "error"})
            return
        
        payload = {"message": f"Knowledge Base Gap Reported:\n{observation}"}
        # Fire and forget the network call
        asyncio.create_task(httpx.AsyncClient(timeout=4.5).post(webhook_url, json=payload))
        call_logger.info(f"Notified Slack about missing knowledge: {observation[:50]}...")

        # Hybrid Speech-Aware Logic:
        # If the bot is already speaking (Parallel mode), wait for it to finish 
        # before returning the result to prevent a speculative turn restart.
        # Use an instructional result to steer the model away from "goodbyes".
        was_speaking = speech_tracker.is_speaking
        if was_speaking:
            await await_bot_silence()
        
        await params.result_callback({
            "status": "success",
            "feedback": "Internal observation logged. Instruction: DO NOT mention this log to the caller. Proceed with your current response naturally."
        })

    async def transfer_call_handler(args: FunctionCallParams):
        nonlocal is_terminating
        phone_number = args.arguments.get("phone_number")
        contact_name = args.arguments.get("contact_name", "a volunteer")
        
        # Guard against hallucinated/placeholder phone numbers (e.g. from cancelled lookups)
        if phone_number and "555" in phone_number.replace("-", "").replace(" ", ""):
            call_logger.warning(f"Rejected hallucinated phone number: {phone_number}")
            return {"status": "error", "message": f"The phone number {phone_number} appears to be invalid. Please use lookup_contact to find the real phone number first."}
        
        call_logger.info(f"Transferring call for {call_data['call_id']} to {contact_name} at {phone_number}")
        
        call_sid = call_data["call_id"]
        pending_transfers[call_sid] = {
            "number": phone_number,
            "name": contact_name
        }
        # A transfer overrides a hangup. Clean up any accidental hangup requests from the LLM.
        if call_sid in pending_hangups:
            pending_hangups.remove(call_sid)
            
        # Start snappy termination task and return success for a farewell
        if not is_terminating:
            is_terminating = True
            asyncio.create_task(wait_and_terminate())
            
        return {"status": "transfer_initiated", "instruction": f"You are now transferring the call to {contact_name}. Briefly inform the user that you are connecting them now. CRITICAL: Do NOT call the end_call tool yourself; the system handles the hangup naturally after the transfer."}

    async def lookup_contact_handler(params: FunctionCallParams):
        contact_name = params.arguments.get("contact_name")
        call_logger.info(f"Bot requesting CiviCRM lookup for: {contact_name}")
        try:
            contacts = await asyncio.wait_for(civicrm_lookup.lookup_contact_by_name(contact_name), timeout=4.5)
            if len(contacts) == 1 and len(contacts[0]["phones"]) == 1:
                contact = contacts[0]
                phone_number = contact["phones"][0]["number"]
                logger.info(f"Unique match found for {contact_name}: {phone_number}.")
                await params.result_callback({"status": "success", "phone_number": phone_number, "message": f"Found {contact_name}."})
                return
            error_msg = civicrm_lookup.format_disambiguation_message(contacts)
            await params.result_callback({"status": "error", "message": error_msg})
        except asyncio.TimeoutError:
            await params.result_callback({"status": "error", "message": "Lookup timed out."})
        except Exception as e:
            await params.result_callback({"status": "error", "message": str(e)})

    async def get_membership_handler(params: FunctionCallParams):
        if not caller_contact_id:
            await params.result_callback({"status": "error", "message": "I don't recognize your phone number."})
            return
        info = await civicrm_agent.get_membership_info(caller_contact_id)
        await params.result_callback({"status": "success", "message": info})

    async def list_info_handler(params: FunctionCallParams):
        if not caller_contact_id:
            await params.result_callback({"status": "error", "message": "Unrecognized caller."})
            return
        summary = await civicrm_agent.list_contact_info(caller_contact_id)
        await params.result_callback({"status": "success", "message": summary})

    async def add_address_handler(params: FunctionCallParams):
        if not caller_contact_id:
            await params.result_callback({"status": "error", "message": "Unrecognized caller."})
            return
        result = await civicrm_agent.add_address(caller_contact_id, params.arguments.get("street_address"), params.arguments.get("city"), params.arguments.get("postal_code"), params.arguments.get("is_primary", False))
        await params.result_callback({"status": "success", "message": result})

    async def add_phone_handler(params: FunctionCallParams):
        if not caller_contact_id:
            await params.result_callback({"status": "error", "message": "Unrecognized caller."})
            return
        result = await civicrm_agent.add_phone(caller_contact_id, params.arguments.get("phone_number"), params.arguments.get("is_primary", False))
        await params.result_callback({"status": "success", "message": result})

    async def add_email_handler(params: FunctionCallParams):
        if not caller_contact_id:
            await params.result_callback({"status": "error", "message": "Unrecognized caller."})
            return
        result = await civicrm_agent.add_email(caller_contact_id, params.arguments.get("email_address"), params.arguments.get("is_primary", False))
        await params.result_callback({"status": "success", "message": result})

    async def set_primary_handler(params: FunctionCallParams):
        if not caller_contact_id:
            await params.result_callback({"status": "error", "message": "Unrecognized caller."})
            return
        result = await civicrm_agent.set_primary_record(params.arguments.get("entity_type"), params.arguments.get("record_id"))
        await params.result_callback({"status": "success", "message": result})

    async def create_contact_handler(params: FunctionCallParams):
        first = (params.arguments.get("first_name") or "").strip()
        last = (params.arguments.get("last_name") or "").strip()
        
        # Guard against sentinel/placeholder names that Gemini may hallucinate
        BLOCKED_NAMES = {"unknown", "caller", "anonymous", "unavailable", "n/a", "none"}
        if first.lower() in BLOCKED_NAMES or last.lower() in BLOCKED_NAMES:
            call_logger.warning(f"Rejected sentinel contact name: {first} {last}")
            await params.result_callback({"status": "error", "message": "That does not appear to be a real name. Please ask the caller for their actual first and last name."})
            return
        
        logger.info(f"Bot creating new contact: {first} {last} for number {caller_number}")
        result_data = await civicrm_agent.create_contact(first, last, caller_number)
        if result_data["success"]:
            nonlocal caller_contact_id
            caller_contact_id = result_data["contact_id"]
            await params.result_callback({"status": "success", "message": result_data["message"]})
        else:
            await params.result_callback({"status": "error", "message": result_data["message"]})

    async def ask_support_bot_handler(params: FunctionCallParams):
        question = params.arguments.get("question", "")
        goclaw_url = os.getenv("GOCLAW_API_URL")
        goclaw_key = os.getenv("GOCLAW_API_KEY")
        goclaw_agent = os.getenv("GOCLAW_AGENT", "10bot")
        
        if not goclaw_url or not goclaw_key:
            call_logger.error("GOCLAW_API_URL or GOCLAW_API_KEY not configured")
            await params.result_callback({"status": "error", "message": "Support bot is not available right now."})
            return
        
        call_logger.info(f"Consulting Goclaw support bot: {question[:80]}...")
        
        # Use caller's phone number as the Goclaw user ID for session continuity
        goclaw_user_id = caller_number if caller_number != "Unknown Caller" else f"call-{call_sid}"
        
        # Event to keep the Pipecat tool task alive
        finished_event = asyncio.Event()
        
        async def _fetch_and_inject():
            """Background task: query Goclaw and inject result into conversation context."""
            try:
                # Give the bot time to speak its stall instruction before we potentially trigger speech again
                await asyncio.sleep(0.5)
                
                if call_sid not in goclaw_sessions:
                    goclaw_sessions[call_sid] = []
                goclaw_sessions[call_sid].append({"role": "user", "content": question})
                
                async with httpx.AsyncClient(timeout=25.0) as client:
                    response = await client.post(
                        f"{goclaw_url.rstrip('/')}/v1/chat/completions",
                        headers={
                            "Authorization": f"Bearer {goclaw_key}",
                            "X-GoClaw-User-Id": goclaw_user_id,
                            "X-GoClaw-Agent": goclaw_agent,
                            "Content-Type": "application/json"
                        },
                        json={
                            "messages": goclaw_sessions[call_sid]
                        }
                    )
                    response.raise_for_status()
                    data = response.json()
                    
                    answer = data.get("choices", [{}])[0].get("message", {}).get("content", "")
                    if not answer:
                        call_logger.warning("Goclaw returned empty response")
                        answer = "ERROR: The support bot returned an empty response. It may not be able to help with this query. Proceed to answer on your own."
                    else:
                        call_logger.info(f"Goclaw responded ({len(answer)} chars)")
                        goclaw_sessions[call_sid].append({"role": "assistant", "content": answer})
                    
            except httpx.TimeoutException:
                call_logger.error("Goclaw request timed out")
                answer = "TIMEOUT: The support bot is taking too long to respond. Proceed to answer on your own."
            except Exception as e:
                call_logger.error(f"Goclaw request failed: {e}")
                answer = "ERROR: The support bot is currently unavailable. Proceed to answer on your own."
            finally:
                # Signal the tool handler task that it can safely finish
                finished_event.set()
            
            # Don't inject if the call is ending
            if is_terminating:
                call_logger.info("Call is terminating, discarding Goclaw response")
                return
            
            # Wait for bot to finish speaking before injecting (same pattern as hangup)
            await await_bot_silence()
            
            msg = {
                "role": "developer",
                "content": f"SUPPORT BOT RESPONSE: The support bot answered the question '{question}' as follows:\n\n{answer}\n\nRelay this information to the caller naturally, in your own voice. Do not mention the support bot or that you looked anything up."
            }
            
            # Inject into local context for bookkeeping
            context.add_message(msg)
            
            # Push directly to GeminiLiveLLMService to bypass aggregators and send to WebSocket
            await llm.process_frame(LLMMessagesAppendFrame([msg]), FrameDirection.DOWNSTREAM)
        
        # Fire off the background task (survives caller interruptions)
        asyncio.create_task(_fetch_and_inject())

        # Return immediately to Pipecat framework so the bot can stall naturally
        await params.result_callback({
            "status": "processing",
            "instruction": "The support bot is looking this up now. Stall naturally while waiting — say something like 'Let me check on that for you' or 'One moment.' The answer will arrive shortly and be provided to you automatically."
        })

        # Wait here to prevent the Pipecat tool handler task from finishing immediately.
        # This works around a Pipecat bug where returning immediately while an interruption
        # occurs causes a 'dictionary changed size during iteration' exception in _cancel_function_call.
        # If the user interrupts, Pipecat safely cancels this task and we catch it here.
        try:
            await finished_event.wait()
        except asyncio.CancelledError:
            call_logger.debug("ask_support_bot_handler cancelled by interruption, background task survives.")
            pass

    llm.register_function("end_call", hang_up, timeout_secs=5.0)
    llm.register_function("report_missing_knowledge", notify_slack, timeout_secs=5.0)
    llm.register_function("transfer_call", transfer_call_handler, timeout_secs=5.0)
    llm.register_function("lookup_contact", lookup_contact_handler, timeout_secs=5.0)
    llm.register_function("check_my_membership", get_membership_handler, timeout_secs=5.0)
    llm.register_function("list_my_contact_info", list_info_handler, timeout_secs=5.0)
    llm.register_function("add_new_address", add_address_handler, timeout_secs=5.0)
    llm.register_function("add_new_phone", add_phone_handler, timeout_secs=5.0)
    llm.register_function("add_new_email", add_email_handler, timeout_secs=5.0)
    llm.register_function("set_info_as_primary", set_primary_handler, timeout_secs=5.0)
    llm.register_function("create_my_contact_record", create_contact_handler, timeout_secs=5.0)
    llm.register_function("ask_support_bot", ask_support_bot_handler, timeout_secs=30.0)

    async def session_warning_task():
        try:
            await asyncio.sleep(420)
            context.add_message({"role": "developer", "content": "SYSTEM WARNING: There are 3 minutes remaining in this call."})
            await asyncio.sleep(60)
            context.add_message({"role": "developer", "content": "SYSTEM WARNING: There are 2 minutes remaining. You must wrap up now."})
            await asyncio.sleep(60)
            context.add_message({"role": "developer", "content": "CRITICAL SYSTEM WARNING: There is only 1 minute remaining. You MUST end the call IMMEDIATELY."})
            await task.queue_frames([LLMRunFrame()])
        except asyncio.CancelledError:
            pass

    warning_task = asyncio.create_task(session_warning_task())

    context = LLMContext()
    user_aggregator, assistant_aggregator = LLMContextAggregatorPair(context)

    pipeline = Pipeline([
        transport.input(),
        user_aggregator,
        llm,
        assistant_aggregator,
        speech_tracker,
        transport.output()
    ])

    task = PipelineTask(pipeline, params=PipelineParams(audio_in_sample_rate=8000, audio_out_sample_rate=8000, enable_metrics=True, enable_usage_metrics=True))

    caller_contact_id = None
    caller_recognized_name = None  # CiviCRM first name, set when caller is recognized

    @transport.event_handler("on_client_connected")
    async def on_client_connected(transport, client):
        logger.info(f"Client connected: {client}")
        now = datetime.now(ZoneInfo("America/Chicago")).strftime("%A, %B %d, %Y at %I:%M %p")
        
        nonlocal caller_contact_id, caller_recognized_name
        contact_info = await civicrm_lookup.lookup_contact_by_phone(caller_number)
        
        detail_block = ""
        if caller_name:
            detail_block = f"CURRENT CALLER INFO: Unrecognized caller identified via CNAM as {caller_name}."
            greeting = f"'Thank you for calling 10BitWorks, San Antonio's largest, member-supported, nonprofit makerspace! Am I speaking with {caller_first_name}?'"
        else:
            greeting = "'Thank you for calling 10BitWorks, San Antonio's largest, member-supported, nonprofit makerspace! Who am I speaking with today?'"
        
        if contact_info:
            caller_contact_id = contact_info["contact_id"]
            name = contact_info["name"]
            caller_recognized_name = name
            
            # Fetch full profile in parallel to reduce handshake latency
            membership, contact_details = await asyncio.gather(
                civicrm_agent.get_membership_info(caller_contact_id),
                civicrm_agent.list_contact_info(caller_contact_id),
                return_exceptions=True
            )
            
            # Handle potential exceptions gracefully
            membership = str(membership) if not isinstance(membership, Exception) else "Membership data unavailable."
            contact_details = str(contact_details) if not isinstance(contact_details, Exception) else "Contact details unavailable."
            
            detail_block = f"CURRENT CALLER INFO: Recognized as {name} (ID: {caller_contact_id}).\n\n{membership}\n\n{contact_details}"
            greeting = f"'Hi {name}! Thank you for calling 10BitWorks, San Antonio's largest, member-supported, nonprofit makerspace! How can I help you today?'"
            
        # Determine the recipient label for Zammad CTI
        recipient_label = "Makerspace"
        if "2105470221" in destination_number:
            recipient_label = "Virtual Receptionist"
        elif "8559042954" in destination_number:
            recipient_label = "Test Line"

        # Trigger Zammad CTI answer event
        asyncio.create_task(zammad_cti.push_cti_event(
            "answer", 
            caller_number, 
            destination_number, 
            "in", 
            call_data["call_id"], 
            user_name=recipient_label,
            answering_number="10Bot"
        ))

        context.add_message(
            {"role": "developer", "content": f"SYSTEM INFO: Your name is {selected_voice}. The current date and time is {now}. The caller's phone number is {caller_number}.\n\n{detail_block}\n\nSimply say: {greeting}"}
        )
        await task.queue_frames([LLMRunFrame()])

    @llm.event_handler("on_error")
    async def on_llm_error(service, error):
        # Force a pipeline exit on fatal service errors to avoid silent hangs
        call_logger.error(f"LLM Service Error: {error}")
        await task.queue_frames([EndFrame()])

    with logger.contextualize(call_id=call_sid):
        runner = PipelineRunner(handle_sigint=True)
        try:
            await runner.run(task)
        except Exception as e:
            call_logger.error(f"Error running pipeline: {e}")
            # Ensure the call doesn't stay open in silence on crash
            await task.queue_frames([EndFrame()])
        finally:
            # Stop the warning task and ensure pipeline task is cancelled
            warning_task.cancel()
            await task.cancel()
            
            # Post-call processing (Transcription and Ticketing)
            try:
                call_logger.info(f"Starting post-call processing for {call_sid}")
                
                # Dump full conversation history to the log at the end
                call_logger.debug("--- FINAL TRANSCRIPT ---")
                for entry in call_history:
                    call_logger.debug(entry)
                call_logger.debug("--- END TRANSCRIPT ---")
                
                # Generate transcript for logging/ticketing
                transcript = ""
                # Determine display name for the caller
                display_user_name = caller_recognized_name or caller_name or "Caller"
                
                role_map = {
                    "assistant": "Receptionist",
                    "model": "Receptionist",
                    "user": display_user_name
                }

                for msg in context.messages:
                    role = msg.get("role", "")
                    content = msg.get("content", "")
                    
                    # Only include User and Assistant roles
                    if role in role_map and content:
                        # Filter out internal protocol JSON or async tool messages
                        if content.strip().startswith("{") or "async_tool" in content:
                            continue
                            
                        display_name = role_map[role]
                        transcript += f"**{display_name}**: {content}\n\n"
                        
                if transcript:
                    call_transcripts[call_sid] = transcript

                if caller_contact_id:
                    if transcript:
                        try:
                            await civicrm_agent.log_call_activity(caller_contact_id, "Inbound Call via 10Bot", f"Call Transcript:\n\n{transcript}")
                            call_logger.info(f"Logged transcript to CiviCRM for contact {caller_contact_id}")
                        except Exception as e:
                            call_logger.error(f"Failed to log transcript to CiviCRM: {e}")
                        
                    # Create Zammad ticket with full transcript
                    customer_id = "10bot@10bitworks.org"  # Fallback customer
                    email = await civicrm_agent.get_contact_email(caller_contact_id)
                    if email:
                        customer_id = email
                    
                    try:
                        ticket = await zammad_agent.create_ticket(
                            title=f"Call Transcript: {caller_number}",
                            body=f"Full transcript of call from {caller_number}:\n\n{transcript if transcript else 'No conversational dialogue recorded.'}",
                            customer=customer_id,
                            owner="10bot@10bitworks.org",
                            article_type="phone"
                        )
                        if ticket:
                            call_logger.info(f"Created Zammad ticket {ticket.get('id')} for recognized caller {customer_id}")
                        else:
                            call_logger.warning("Zammad ticket creation returned None.")
                    except Exception as e:
                        call_logger.error(f"Failed to create Zammad ticket for recognized caller: {e}")
                else:
                    # Truly unrecognized callers (no CiviCRM contact)
                    try:
                        ticket = await zammad_agent.create_ticket(
                            title=f"Call Transcript (Unrecognized): {caller_number}",
                            body=f"Full transcript of call from {caller_number}:\n\n{transcript if transcript else 'No conversational dialogue recorded.'}",
                            customer="10bot@10bitworks.org",
                            owner="10bot@10bitworks.org",
                            article_type="phone"
                        )
                        if ticket:
                            call_logger.info(f"Created Zammad ticket {ticket.get('id')} for unrecognized caller")
                        else:
                            call_logger.warning("Zammad ticket creation returned None.")
                    except Exception as e:
                        call_logger.error(f"Failed to create Zammad ticket for unrecognized caller: {e}")
            except Exception as e:
                call_logger.error(f"Critical error in post-call processing: {e}")
            
            # Final cleanup
            try:
                await websocket.close()
            except RuntimeError:
                pass
            
            # Remove the call-specific log sink last
            logger.remove(handler_id)

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000)
