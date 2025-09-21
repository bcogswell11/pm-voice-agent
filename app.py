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
    NOTE: No 'OpenAI-Beta' header here.
    """
    # If you set OPENAI_REALTIME_MODEL in Heroku, e.g. 'gpt-realtime', we’ll use it.
    model = OPENAI_REALTIME_MODEL or "gpt-realtime"
    url = f"wss://api.openai.com/v1/realtime?model={model}"

    headers = {
        "Authorization": f"Bearer {OPENAI_API_KEY}",
        # IMPORTANT: Do NOT include the beta header in GA
        # "OpenAI-Beta": "realtime=v1",
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

async def twilio_to_openai(twilio_ws, openai_ws, stream_info):
    """
    Greeting-first test: capture Twilio streamSid and IGNORE input audio.
    Also: make Twilio receive non-blocking so it can't freeze the event loop.
    """
    while True:
        # Non-blocking read of Twilio WS
        try:
            msg = await asyncio.to_thread(twilio_ws.receive)
        except Exception:
            print("[twilio->openai] receive() raised -> break")
            break

        if msg is None:
            print("[twilio->openai] ws.receive() returned None -> break")
            break

        # Parse Twilio event
        try:
            data = json.loads(msg)
        except Exception:
            continue

        evt = data.get("event")

        # Capture streamSid once at call start
        if evt == "start":
            try:
                stream_info["sid"] = data.get("start", {}).get("streamSid")
                print(f"[twilio] start streamSid={stream_info['sid']}")
            except Exception:
                pass
            continue

        # For this step, ignore 'media' (we're just testing outbound audio)
        if evt == "media":
            continue

        if evt == "stop":
            print("[twilio->openai] received stop -> break")
            break

        # ignore other Twilio events
        continue

    return


async def openai_to_twilio(twilio_ws, openai_ws, stream_info):
    """
    Read audio deltas from OpenAI and send to Twilio as media frames.
    Always include streamSid. Handles multiple audio event shapes.
    Also logs short text deltas to confirm the model is replying.
    """
    def send_to_twilio(b64audio: str):
        sid = stream_info.get("sid")
        if not sid:
            # Twilio discards media without streamSid
            print("[twilio<-openai] SKIP (no streamSid yet)")
            return
        msg = {
            "event": "media",
            "streamSid": sid,
            "media": {"payload": b64audio}
        }
        try:
            twilio_ws.send(json.dumps(msg))
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

            # Text delta visibility (sometimes the model produces both text and audio)
            if t in ("response.output_text.delta", "response.text.delta"):
                try:
                    snippet = evt.get("delta", "")
                    if isinstance(snippet, dict):
                        snippet = snippet.get("text", "")
                    print(f"[openai] text δ: {str(snippet)[:120]}")
                except Exception:
                    pass

            # If OpenAI reports an error, dump and stop this reader
            if t == "error":
                try:
                    print("[openai] ERROR RAW=" + json.dumps(evt))
                except Exception:
                    print(f"[openai] ERROR RAW={evt}")
                return

            # Extract audio payload from known shapes
            b64audio = None

            # 1) Common realtime shapes
            if t in ("response.audio.delta", "response.output_audio.delta"):
                b64audio = evt.get("delta")

            # 2) Some previews: 'delta' is an object with 'audio'
            if not b64audio and isinstance(evt.get("delta"), dict):
                b64audio = evt["delta"].get("audio")

            # 3) Rare older shapes: 'data' dict contains 'audio'
            if not b64audio and isinstance(evt.get("data"), dict):
                b64audio = evt["data"].get("audio")

            # 4) Output-item deltas that carry audio inside delta
            if not b64audio and t in ("response.output_item.delta", "response.delta"):
                maybe = evt.get("delta")
                if isinstance(maybe, dict):
                    b64audio = maybe.get("audio")

            # Forward audio chunk to Twilio (with streamSid)
            if b64audio:
                send_to_twilio(b64audio)

            # Optional marker; keep socket open
            if t in ("response.completed", "response.audio.done", "response.stop"):
                sid = stream_info.get("sid")
                if sid:
                    try:
                        twilio_ws.send(json.dumps({"event": "mark", "streamSid": sid, "mark": {"name": "openai_done"}}))
                    except Exception:
                        pass
                # do not break
    except Exception:
        pass

    # Politely stop Twilio stream when done (if we have sid)
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

        # GA session schema (matches Twilio’s working examples)
        session_update = {
            "type": "session.update",
            "session": {
                "type": "realtime",
                "model": OPENAI_REALTIME_MODEL or "gpt-realtime",
                "output_modalities": ["audio"],  # tell server we want audio out
                "audio": {
                    "input": {
                        "format": {"type": "audio/pcmu"},           # Twilio = G.711 µ-law (pcmu)
                        "turn_detection": {"type": "server_vad"}    # server VAD
                    },
                    "output": {
                        "format": {"type": "audio/pcmu"},
                        "voice": OPENAI_VOICE or "alloy"
                    }
                },
                "instructions": (
                    "You are a friendly property management assistant. "
                    "Keep replies short and helpful unless asked for details."
                ),
            }
        }
        loop.run_until_complete(openai_ws.send(json.dumps(session_update)))
        print("[stream] sent session.update (GA, audio/pcmu)")

        # Start the OpenAI->Twilio reader FIRST so we don't miss audio
        stream_info = {"sid": None}  # filled from Twilio 'start' event
        recv_task = loop.create_task(openai_to_twilio(ws, openai_ws, stream_info))
        loop.run_until_complete(asyncio.sleep(0))  # yield so task is active

        # Send a greeting as an immediate response (GA: instructions inside response)
        hello = {
            "type": "response.create",
            "response": {
                # GA can stream audio without listing text, but if your account requires both,
                # you can use ["audio", "text"]. Try audio-only first per GA docs.
                "modalities": ["audio"],
                "instructions": (
                    "Welcome to Escallop Property Management. "
                    "I can take a maintenance request, answer general questions, "
                    "or forward you to a live person."
                )
            }
        }
        loop.run_until_complete(openai_ws.send(json.dumps(hello)))
        print("[stream] sent response.create (greeting)")

        # Now start the Twilio->OpenAI sender
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
