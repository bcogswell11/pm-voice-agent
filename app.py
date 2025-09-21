# --- Imports ---
from flask import Flask, Response
from flask_sock import Sock
import os
import json
import base64
import threading
import time
import asyncio
import websockets
import audioop


# --- Config (from Heroku Config Vars) ---
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY", "")
OPENAI_REALTIME_MODEL = os.getenv("OPENAI_REALTIME_MODEL", "gpt-4o-realtime-preview")
OPENAI_VOICE = os.getenv("OPENAI_VOICE", "alloy")
PUBLIC_BASE_URL = os.getenv("PUBLIC_BASE_URL", "").rstrip("/")

# --- App setup ---
app = Flask(__name__)
sock = Sock(app)

# --- Health check ---
@app.get("/health")
def health():
    return {"status": "ok"}

# --- Safe default: Say-only webhook (keeps production stable) ---
@app.post("/voice")
def voice():
    twiml = """<?xml version="1.0" encoding="UTF-8"?>
<Response>
  <Say voice="Polly.Matthew">Streaming is not turned on yet. This is a test message to confirm the webhook works.</Say>
</Response>"""
    return Response(twiml, mimetype="text/xml")

# --- Streaming test webhook (TwiML) ---
@app.post("/voice_stream_test")
def voice_stream_test():
    # Build secure wss:// URL to our /stream endpoint
    if PUBLIC_BASE_URL.startswith("https://"):
        stream_url = "wss://" + PUBLIC_BASE_URL[len("https://"):] + "/stream"
    elif PUBLIC_BASE_URL.startswith("http://"):
        stream_url = "ws://" + PUBLIC_BASE_URL[len("http://"):] + "/stream"
    else:
        stream_url = "wss://escallop-voice-pm-aa62425200e7.herokuapp.com/stream"

    twiml = f"""<?xml version="1.0" encoding="UTF-8"?>
<Response>
  <Connect>
    <Stream url="{stream_url}"/>
  </Connect>
</Response>"""
    return Response(twiml, mimetype="text/xml")

# ------------- Twilio <-> OpenAI relay helpers -------------

async def openai_realtime_connect():
    """
    Connect to OpenAI Realtime WS.
    """
    url = f"wss://api.openai.com/v1/realtime?model={OPENAI_REALTIME_MODEL}&voice={OPENAI_VOICE}"
    headers = {
        "Authorization": f"Bearer {OPENAI_API_KEY}",
        "OpenAI-Beta": "realtime=v1",
    }
    ws = await websockets.connect(url, extra_headers=headers, ping_interval=20, ping_timeout=20, max_size=None)
    return ws

def _safe_get(d, *keys, default=None):
    cur = d
    for k in keys:
        if not isinstance(cur, dict) or k not in cur:
            return default
        cur = cur[k]
    return cur

async def twilio_to_openai(twilio_ws, openai_ws):
    """
    Greeting-only test: do NOT touch OpenAI's input buffer at all.
    We just read Twilio events and ignore them. No clear, no append, no commit.
    """
    while True:
        msg = twilio_ws.receive()
        if msg is None:
            print("[twilio->openai] ws.receive() returned None -> break")
            break

        try:
            data = json.loads(msg)
        except Exception:
            continue

        if data.get("event") == "stop":
            print("[twilio->openai] received stop -> break")
            break

    return




async def openai_to_twilio(twilio_ws, openai_ws):
    """
    Read audio from OpenAI and send to Twilio as media frames.
    Logs event types and prints the FULL error payload when present.
    """
    try:
        async for raw in openai_ws:
            try:
                evt = json.loads(raw)
            except Exception:
                continue

            t = evt.get("type")
            # Always log the event type
            try:
                print(f"[openai] evt={t}")
            except Exception:
                pass

            # If OpenAI reports an error, dump the full JSON and stop this reader
            if t == "error":
                try:
                    print("[openai] ERROR RAW=" + json.dumps(evt))
                except Exception:
                    print(f"[openai] ERROR RAW={evt}")
                return  # <-- stop the loop so we don't spam

            # Audio chunk handling
            b64audio = None
            if t in ("response.audio.delta", "response.output_audio.delta"):
                b64audio = evt.get("delta")

            if not b64audio:
                b64audio = (
                    evt.get("audio")
                    or (evt.get("delta", {}).get("audio") if isinstance(evt.get("delta"), dict) else None)
                    or (evt.get("data", {}).get("audio") if isinstance(evt.get("data"), dict) else None)
                )

            if b64audio:
                try:
                    twilio_ws.send(json.dumps({"event": "media", "media": {"payload": b64audio}}))
                except Exception:
                    break

            # Keep the socket open; don't force-break on completion
            if t in ("response.completed", "response.audio.done", "response.stop"):
                try:
                    twilio_ws.send(json.dumps({"event": "mark", "mark": {"name": "openai_done"}}))
                except Exception:
                    pass
                # Do not break here
    except Exception:
        pass

    try:
        twilio_ws.send(json.dumps({"event": "stop"}))
    except Exception:
        pass




# --- WebSocket: Twilio connects here ---
@sock.route("/stream")
def stream(ws):
    if not OPENAI_API_KEY:
        try:
            ws.send(json.dumps({"event": "stop"}))
        except Exception:
            pass
        return

    loop = asyncio.new_event_loop()

    def runner():
        asyncio.set_event_loop(loop)
        openai_ws = loop.run_until_complete(openai_realtime_connect())

        # Tell OpenAI to speak in μ-law/8k for Twilio compatibility + request audio modality + voice
        session_update = {
            "type": "session.update",
            "session": {
                "modalities": ["audio", "text"],        # text is required with audio
                "voice": OPENAI_VOICE,                   # alloy/coral/sage/verse/etc.
                "input_audio_format": "pcm16",      # <— was g711_ulaw
                "output_audio_format": "g711_ulaw"  # keep μ-law for Twilio playback
            }
        }
        loop.run_until_complete(openai_ws.send(json.dumps(session_update)))
        print("[stream] sent session.update for g711_ulaw/8k")

        # Immediate greeting so caller hears voice
        hello = {
            "type": "response.create",
            "response": {
                "instructions": (
                    "You are a friendly property management assistant. "
                    "Greet the caller and let them know you can take maintenance requests, "
                    "answer general questions, or forward to a live person."
                ),
                "modalities": ["audio", "text"],   # ensure audio is produced
            }
        }
        loop.run_until_complete(openai_ws.send(json.dumps(hello)))

        # Start relays
        stream_info = {"sid": None}  # holds Twilio streamSid
        send_task = loop.create_task(twilio_to_openai(ws, openai_ws, stream_info))
        recv_task = loop.create_task(openai_to_twilio(ws, openai_ws, stream_info))

        # Wait for either side to finish
        loop.run_until_complete(asyncio.gather(send_task, recv_task, return_exceptions=True))

        # Clean up
        try:
            loop.run_until_complete(openai_ws.close())
        except Exception:
            pass

    t = threading.Thread(target=runner, daemon=True)
    t.start()

    try:
        while t.is_alive():
            time.sleep(0.05)
    except Exception:
        pass


# --- Main (local only) ---
if __name__ == "__main__":
    app.run(host="0.0.0.0", port=8000)
