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
    Connect to OpenAI Realtime WS (GA schema).
    """
    model = OPENAI_REALTIME_MODEL or "gpt-realtime"
    url = f"wss://api.openai.com/v1/realtime?model={model}"
    headers = {
        "Authorization": f"Bearer {OPENAI_API_KEY}",
    }
    ws = await websockets.connect(
        url,
        extra_headers=headers,
        ping_interval=20,
        ping_timeout=20,
        max_size=None
    )
    return ws

def _safe_get(d, *keys, default=None):
    cur = d
    for k in keys:
        if not isinstance(cur, dict) or k not in cur:
            return default
        cur = cur[k]
    return cur

async def twilio_to_openai(twilio_ws, openai_ws, stream_info):
    """
    Minimal: capture Twilio streamSid from 'start'. Ignore media (no input).
    Non-blocking reads so we don't freeze the loop.
    """
    while True:
        try:
            msg = await asyncio.to_thread(twilio_ws.receive)
        except Exception:
            print("[twilio->openai] receive() raised -> break")
            break
        if msg is None:
            print("[twilio->openai] ws.receive() returned None -> break")
            break

        try:
            data = json.loads(msg)
        except Exception:
            continue

        evt = data.get("event")
        if evt == "start":
            try:
                stream_info["sid"] = data.get("start", {}).get("streamSid")
                print(f"[twilio] start streamSid={stream_info['sid']}")
            except Exception:
                pass
        elif evt == "stop":
            print("[twilio->openai] received stop -> break")
            break
        # ignore other events
    return


async def openai_to_twilio(twilio_ws, openai_ws, stream_info):
    """
    Read OpenAI audio deltas and send to Twilio as media frames (with streamSid).
    """
    def send_to_twilio(b64audio: str):
        sid = stream_info.get("sid")
        if not sid:
            print("[twilio<-openai] SKIP (no streamSid yet)")
            return
        # Normalize base64 payload (safe no-op if already normalized)
        try:
            payload = base64.b64encode(base64.b64decode(b64audio)).decode("utf-8")
        except Exception:
            payload = b64audio
        try:
            twilio_ws.send(json.dumps({
                "event": "media",
                "streamSid": sid,
                "media": {"payload": payload}
            }))
        except Exception:
            pass

    try:
        async for raw in openai_ws:
            try:
                evt = json.loads(raw)
            except Exception:
                continue

            t = evt.get("type")
            print(f"[openai] evt={t}")

            # audio delta shapes we care about
            b64audio = None
            if t in ("response.output_audio.delta", "response.audio.delta"):
                b64audio = evt.get("delta")
            if not b64audio and isinstance(evt.get("delta"), dict):
                b64audio = evt["delta"].get("audio")
            if not b64audio and isinstance(evt.get("data"), dict):
                b64audio = evt["data"].get("audio")
            if not b64audio and t in ("response.output_item.delta", "response.delta"):
                maybe = evt.get("delta")
                if isinstance(maybe, dict):
                    b64audio = maybe.get("audio")

            if b64audio:
                send_to_twilio(b64audio)

            if t == "error":
                try:
                    print("[openai] ERROR RAW=" + json.dumps(evt))
                except Exception:
                    print(f"[openai] ERROR RAW={evt}")
                return
    except Exception:
        pass

    sid = stream_info.get("sid")
    if sid:
        try:
            twilio_ws.send(json.dumps({"event": "stop", "streamSid": sid}))
        except Exception:
            pass

# --- WebSocket: Twilio connects here ---
# --- WebSocket: Twilio connects here ---
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

        # GA session: ask for audio out; Twilio uses PCMU (G.711 μ-law) at 8 kHz
        session_update = {
            "type": "session.update",
            "session": {
                "type": "realtime",
                "model": OPENAI_REALTIME_MODEL or "gpt-realtime",
                "output_modalities": ["audio"],
                "audio": {
                    "input": {
                        "format": {"type": "audio/pcmu"},
                        "turn_detection": {"type": "server_vad"}
                    },
                    "output": {
                        "format": {"type": "audio/pcmu"},
                        "voice": OPENAI_VOICE or "alloy"
                    }
                },
                "instructions": "Be concise and friendly."
            }
        }
        loop.run_until_complete(openai_ws.send(json.dumps(session_update)))
        print("[stream] sent session.update (GA, audio/pcmu)")

        # Start the OpenAI->Twilio reader FIRST so we don't miss audio
        stream_info = {"sid": None}  # filled from Twilio 'start' in twilio_to_openai
        recv_task = loop.create_task(openai_to_twilio(ws, openai_ws, stream_info))
        loop.run_until_complete(asyncio.sleep(0))  # tiny yield so task is active

        # Minimal one-sentence audio test
        # 1) Put a user message into the conversation telling the model what to say
        greet_item = {
            "type": "conversation.item.create",
            "item": {
                "type": "message",
                "role": "user",
                "content": [
                    {"type": "input_text",
                     "text": "Say exactly: Hello! This is a quick audio test."}
                ]
            }
        }
        loop.run_until_complete(openai_ws.send(json.dumps(greet_item)))
        print("[stream] sent conversation.item.create (test line)")

        # 2) Trigger generation (GA: no response.modalities field)
        loop.run_until_complete(openai_ws.send(json.dumps({"type": "response.create"})))
        print("[stream] sent response.create (test)")

        # Now start the Twilio->OpenAI sender (captures streamSid; we ignore media)
        send_task = loop.create_task(twilio_to_openai(ws, openai_ws, stream_info))

        # Wait for both relays to finish
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
