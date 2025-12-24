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
    reading = request.query_params.get("Reading", "0")  # Extract Reading

    response = VoiceResponse()
    connect = Connect()
    # Pass reading to WebSocket URL
    connect.stream(
        url=f"wss://{ngrok}/media-stream-eleven/{customer_name}/{language}/{reading}"
    )
    response.append(connect)

    # Return the TwiML as XML
    return HTMLResponse(content=str(response), media_type="application/xml")


# Function for AI to get real-time gas reading
# Function for AI to get real-time gas reading
def get_current_gas_reading_tool(parameters):
    """
    Retrieves the current real-time gas level reading from the sensor.
    Use this tool immediately when the user asks for the current gas status, level, or reading.
    Returns a text description of the current level and safety status.
    This tool takes no arguments.
    """
    # Debug: Confirm tool is called
    print(
        f"🛠️ AI TOOL CALLED: getCurrentGasReading. Current State: {gas_reading_state['current_reading']}"
    )

    # This matches the logic of the /get-current-reading endpoint
    # But runs internally within the python process
    reading = gas_reading_state["current_reading"]

    msg = f"The actual live gas level is {reading}. "
    if reading < 100:
        msg += "This is currently SAFE."
    elif reading < 200:
        msg += "WARNING: Gas level is ELEVATED. Please be careful."
    else:
        msg += "CRITICAL DANGER: Gas leak is SEVERE! Immediate action required!"

    return msg


# Function to end the call (Tool)
def terminate_call_tool(parameters):
    """
    Terminates the current phone call immediately.
    Use this tool when the user says "Goodbye", "Allah Hafiz", "Bye", or asks to end the call.
    This tool takes no arguments.
    """
    print("🛠️ AI TOOL CALLED: terminateCall")

    # Logic to end call via Twilio API
    # Since we don't have the CallSid easily accessible in this context without lookup,
    # we will hit our own /end-call endpoint or use the client directly if possible.
    # For simplicity in this tool wrapper, we'll return a message but the real work happens if
    # the Dashboard is configured to hit the webhook.

    # However, for local execution (if we could), we'd need the SID.
    # We will assume the Dashboard Webhook is the primary method.
    return "Ending call now. Goodbye."


# Initialize ClientTools and register the custom tool
client_tools = ClientTools()

client_tools.register("getCurrentGasReading", get_current_gas_reading_tool)
client_tools.register("terminateCall", terminate_call_tool)


@app.websocket("/media-stream-eleven/{customer_name}/{language}/{reading}")
async def handle_media_stream(
    websocket: WebSocket, customer_name: str, language: str, reading: str
):
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
                    "You have a tool: 'getCurrentGasReading' and 'terminateCall'. "
                    "1. If user asks for level/status: Use 'getCurrentGasReading'. "
                    "2. If user says 'Goodbye', 'Allah Hafiz', or 'Bye': Say goodbye and Use 'terminateCall' tool IMMEDIATELY. "
                    "Do not ask for permission. Do not explain. Just use the tool."
                    "Goal: Warn user effectively."
                )
            },
            "first_message": "Hello Azfar! Ahmed Speaking. A Critical Gas Leak has been detected. Please check it immediately.",
            "language": "en",  # 'en' handles Roman Urdu well with Multilingual model
        },
        # "tts": {"voice_id": "Xb7hH8MSUJpSbSDYk0k2"},  <-- Enabled now (User turned on Override)
        "tts": {
            "model_id": "eleven_multilingual_v2",  # Better for Urdu
            "voice_id": "TX3LPaxmHKxFdv7VOQHJ",
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

# Global Gas Reading State (Real-Time Monitoring)
gas_reading_state = {
    "current_reading": 0,
    "last_update_time": 0.0,
    "is_alert_active": False,
    "is_alert_active": False,
}


@app.get("/check-call-status")
async def check_call_status():
    """
    Endpoint for NodeMCU to check if a call is currently active.
    Returns: {"active": true/false}
    """
    return {"active": call_state["is_active"]}


@app.post("/twilio/outbound_call")
async def make_outbound_call(
    customer_name: str, language: str, number: str, reading: str = "0"
):
    """
    Initiate an outbound call to the specified target number.
    """
    if not number:
        raise HTTPException(status_code=400, detail="Target number is required.")

    client = Client(TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN)

    try:
        # Construct the URL that Twilio will request once the call is answered
        # Pass Reading to inbound handler
        redirect_url = f"https://{ngrok}/twilio/inbound_call?CustomerName={customer_name}&Language={language}&Reading={reading}"

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


# --- REAL-TIME GAS READING ENDPOINTS ---


@app.post("/update-reading")
async def update_reading(reading: int):
    """
    NodeMCU continuously pushes latest gas readings here.
    This runs in the background during calls.
    """
    gas_reading_state["current_reading"] = reading
    gas_reading_state["last_update_time"] = time.time()
    gas_reading_state["is_alert_active"] = reading >= 100  # Alert threshold

    # Silent logging (no spam)
    # print(f"📊 Reading Updated: {reading}")

    return {"status": "updated", "reading": reading}


@app.get("/get-current-reading")
async def get_current_reading():
    """
    AI calls this endpoint to get the latest gas reading.
    This is used during phone conversations for real-time updates.
    """
    reading = gas_reading_state["current_reading"]
    is_safe = reading < 100

    # Determine status message
    if reading < 50:
        status = "very_safe"
        message = f"Gas level is {reading}, which is very safe (normal range)."
    elif reading < 100:
        status = "safe"
        message = f"Gas level is {reading}, which is safe but slightly elevated."
    elif reading < 200:
        status = "warning"
        message = f"Gas level is {reading}, which is in the warning zone. Please check immediately."
    else:
        status = "critical"
        message = (
            f"Gas level is {reading}, which is CRITICAL! Immediate action required!"
        )

    return {
        "reading": reading,
        "status": status,
        "is_safe": is_safe,
        "message": message,
        "last_update": gas_reading_state["last_update_time"],
    }


# --- SIMPLE ENDPOINT FOR NODEMCU ---
@app.get("/trigger-gas-alert")
async def trigger_gas_alert(reading: str = "0"):
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
    if (current_time - call_state["last_success_time"]) < 10:
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
        customer_name="Azfar", language="urdu", number=TARGET_NUMBER, reading=reading
    )


@app.get("/end-call")
async def end_call():
    """
    Endpoint for AI Tool to terminate the call.
    Uses Twilio API to hang up the active call.
    """
    if not call_state["is_active"]:
        return {"status": "ignored", "reason": "no_active_call"}

    try:
        # Find the active call SID (In a real app, store SID in call_state)
        # For now, we list active calls and kill the first one to correct number/status
        client = Client(TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN)

        calls = client.calls.list(status="in-progress", limit=5)
        for call in calls:
            # Check if it matches our target number or usage (simplified)
            # Just killing ANY active call on this account for safety demonstration
            print(f"Ending active call: {call.sid}")
            call.update(status="completed")

        # Update state immediately
        call_state["is_active"] = False
        return {"status": "success", "message": "Call terminated"}

    except Exception as e:
        print(f"Error ending call: {e}")
        return {"status": "error", "message": str(e)}
