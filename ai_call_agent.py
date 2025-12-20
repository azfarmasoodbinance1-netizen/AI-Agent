import json
import os
import traceback
from dotenv import load_dotenv
from fastapi import FastAPI, Request, WebSocket, HTTPException
from fastapi.responses import HTMLResponse
from pydantic import BaseModel
from twilio.twiml.voice_response import VoiceResponse, Connect
from elevenlabs import ElevenLabs
from elevenlabs.conversational_ai.conversation import (
    Conversation,
    ClientTools,
    ConversationInitiationData,
)
from twilio_audio_interface import TwilioAudioInterface
from starlette.websockets import WebSocketDisconnect
from twilio.rest import Client

# Load environment variables from .env (if needed)
load_dotenv()

# Replace with your actual keys/tokens (hard-coded for demo)
ELEVENLABS_API_KEY = os.getenv("ELEVENLABS_API_KEY", "")
ELEVENLABS_AGENT_ID = os.getenv("ELEVENLABS_AGENT_ID", "")

TWILIO_ACCOUNT_SID = os.getenv("TWILIO_ACCOUNT_SID", "")
TWILIO_AUTH_TOKEN = os.getenv("TWILIO_AUTH_TOKEN", "")
TWILIO_PHONE_NUMBER = os.getenv("TWILIO_PHONE_NUMBER", "")

# ngrok or any publicly accessible domain / tunnel
ngrok = os.getenv("NGROK_URL", "")


app = FastAPI()


# Pydantic model for inbound JSON data
class CustomerDetails(BaseModel):
    customer_name: str
    language: str


@app.get("/")
async def root():
    return {"message": "Twilio-ElevenLabs Integration Server"}


@app.post("/twilio/inbound_call")
async def handle_incoming_call(request: Request):
    """
    Handles incoming Twilio calls and dynamically uses customer_name and language.
    """
    # Extract query parameters
    customer_name = request.query_params.get("CustomerName", "Unknown")
    language = request.query_params.get("Language", "en")

    form_data = await request.form()
    call_sid = form_data.get("CallSid", "Unknown")
    from_number = form_data.get("From", "Unknown")

    print(
        f"Incoming or answered outbound call: CallSid={call_sid}, "
        f"From={from_number}, CustomerName={customer_name}, Language={language}"
    )

    # Generate a valid TwiML response that starts the <Connect><Stream>
    response = VoiceResponse()
    connect = Connect()
    connect.stream(url=f"wss://{ngrok}/media-stream-eleven/{customer_name}/{language}")
    response.append(connect)

    # Return the TwiML as XML
    return HTMLResponse(content=str(response), media_type="application/xml")


# Function to trigger browser alert
def trigger_browser_alert(parameters):
    message = parameters.get("message")
    print(f"Triggering alert: {message}")
    return "Alert triggered successfully"


# Initialize ClientTools and register the custom tool
client_tools = ClientTools()
client_tools.register("triggerBrowserAlert", trigger_browser_alert)


@app.websocket("/media-stream-eleven/{customer_name}/{language}")
async def handle_media_stream(websocket: WebSocket, customer_name: str, language: str):
    """
    WebSocket endpoint for handling media streams dynamically based on customer_name and language.
    """
    await websocket.accept()
    print(f"WebSocket connection opened for {customer_name} in {language} language.")

    audio_interface = TwilioAudioInterface(websocket)
    eleven_labs_client = ElevenLabs(api_key=ELEVENLABS_API_KEY)

    # Configure the ElevenLabs conversation dynamically
    conversation_override = {
        "agent": {
            "prompt": {
                "prompt": (
                    "You are 'Ahmed', a smart home safety assistant. "
                    "A CRITICAL GAS LEAK has been detected in the kitchen. "
                    "Your goal is to warn the user (Azfar) immediately in Roman Urdu/English mix. "
                    "Be urgent, clear, and concise. "
                    "Example: 'Hello Azfar, Ahmed Speaking. Kitchen mein Gas Leak detect hua hai! Please foran check karein.' "
                    "Do not panic, but emphasize urgency. Return to normal tone if user says it's fixed."
                )
            },
            "first_message": "Hello Azfar! Ahmed Speaking. Kitchen mein Gas Leak detect hua hai! Please foran check karein.",
            "language": "en",  # 'en' handles Roman Urdu well with Multilingual model
        },
        # "tts": {"voice_id": "Xb7hH8MSUJpSbSDYk0k2"},  <-- Enabled now (User turned on Override)
        "tts": {
            "model_id": "eleven_multilingual_v2",  # Better for Urdu
            "voice_id": "Xb7hH8MSUJpSbSDYk0k2",
            "output_format": "ulaw_8000",  # REQUIRED for Twilio (Restored)
            "voice_settings": {
                "stability": 0.5,  # Higher = More consistent/stable tone
                "similarity_boost": 0.7,  # Lower = Less robotic artifacts
            },
        },
    }
    config = ConversationInitiationData(
        conversation_config_override=conversation_override
    )

    # Conversation log in memory
    conversation_log = []

    def on_agent_response(text: str):
        print(f"Agent: {text}")
        conversation_log.append({"speaker": "agent", "message": text})

    def on_user_transcript(text: str):
        print(f"User: {text}")
        conversation_log.append({"speaker": "user", "message": text})

    try:
        conversation = Conversation(
            client=eleven_labs_client,
            agent_id=ELEVENLABS_AGENT_ID,
            requires_auth=False,  # FIXED: Set to False to bypass Signed URL permission error
            audio_interface=audio_interface,
            client_tools=client_tools,
            config=config,
            callback_agent_response=on_agent_response,
            callback_user_transcript=on_user_transcript,
        )

        # Start the conversation session
        conversation.start_session()
        print("Conversation session started.")

        # Continuously receive media stream data from Twilio
        async for message in websocket.iter_text():
            if message:
                await audio_interface.handle_twilio_message(json.loads(message))

    except WebSocketDisconnect:
        print("WebSocket disconnected.")
    except Exception as e:
        print(f"Error in WebSocket handler: {e}")
        traceback.print_exc()
    finally:
        try:
            conversation.end_session()
            conversation.wait_for_session_end()
            print("Conversation session ended.")
        except Exception as e:
            print(f"Error ending conversation session: {e}")
            traceback.print_exc()


import time

# Global Call State
call_state = {
    "is_active": False,
    "last_call_time": 0.0,
    "last_success_time": 0.0,  # To track if user actually picked up
}


@app.post("/twilio/outbound_call")
async def make_outbound_call(customer_name: str, language: str, number: str):
    """
    Initiate an outbound call to the specified target number.
    """
    if not number:
        raise HTTPException(status_code=400, detail="Target number is required.")

    client = Client(TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN)

    try:
        # Construct the URL that Twilio will request once the call is answered
        redirect_url = f"https://{ngrok}/twilio/inbound_call?CustomerName={customer_name}&Language={language}"

        # Callback to track call status (completed, no-answer, etc.)
        status_callback_url = f"https://{ngrok}/twilio/call-status"

        # Generate TwiML using VoiceResponse
        twiml_response = VoiceResponse()
        twiml_response.redirect(redirect_url, method="POST")

        # Initiate the outbound call
        call = client.calls.create(
            twiml=str(twiml_response),
            to=number,
            from_=TWILIO_PHONE_NUMBER,
            status_callback=status_callback_url,
            status_callback_event=[
                "completed",
                "busy",
                "no-answer",
                "failed",
                "canceled",
            ],
        )

        # Mark call as active
        call_state["is_active"] = True
        call_state["last_call_time"] = time.time()

        print(f"Outbound call initiated: {call.sid}")
        return {"message": "Outbound call initiated", "CallSid": call.sid}
    except Exception as e:
        print(f"Error initiating outbound call: {e}")
        call_state["is_active"] = False  # Reset on failure
        raise HTTPException(status_code=500, detail=str(e))


@app.post("/twilio/call-status")
async def call_status_webhook(request: Request):
    """
    Webhook to handle Twilio call status updates.
    Resets the active flag when call ends.
    """
    form_data = await request.form()
    call_status = form_data.get("CallStatus")
    print(f"Call Status Update: {call_status}")

    if call_status == "completed":
        # User picked up and talked. Set success lock for 15 minutes.
        call_state["last_success_time"] = time.time()
        call_state["is_active"] = False
        print("✅ Call Completed Successfully. Muting alarms for 15 minutes.")

    elif call_status in ["busy", "no-answer", "failed", "canceled"]:
        # Call failed. Ready for retry after short cooldown.
        call_state["is_active"] = False
        print("❌ Call Failed/Missed. System ready for retry.")

    return {"status": "ok"}


# --- SIMPLE ENDPOINT FOR NODEMCU ---
@app.get("/trigger-gas-alert")
async def trigger_gas_alert():
    """
    Simple endpoint for NodeMCU to call.
    Includes Spam Prevention & Smart Retry Logic.
    """
    current_time = time.time()

    # 1. Check if call is already in progress
    if call_state["is_active"]:
        # print("⚠️ IGNORING ALERT: Call already in progress.") <--- Silenced as per request
        return {"status": "ignored", "reason": "call_in_progress"}

    # 2. Check: Did we JUST talk to the user? (Smart Mute)
    # If user picked up in last 15 minutes (900s), believe they are fixing it.
    if (current_time - call_state["last_success_time"]) < 900:
        # print("🛡️ IGNORING ALERT: User already acknowledged.") <--- Silenced
        return {"status": "ignored", "reason": "already_acknowledged"}

    # 3. Check for 30-second Retry Cooldown (For failed calls or spam protection)
    if (current_time - call_state["last_call_time"]) < 30:
        # print("⏳ IGNORING ALERT: Cooldown active.") <--- Silenced
        return {"status": "ignored", "reason": "cooldown_active"}

    # HARDCODED TARGET NUMBER (Replace with actual number)
    TARGET_NUMBER = "+923442862596"

    print("⚠️ GAS ALERT RECEIVED! Initiating Call...")

    return await make_outbound_call(
        customer_name="Azfar", language="urdu", number=TARGET_NUMBER
    )
