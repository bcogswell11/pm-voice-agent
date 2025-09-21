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
import time
from datetime import datetime

BOOT_TS = time.monotonic()

def log(tag, **fields):
    # Compact single-line log with monotonic ms since boot
    ms = int((time.monotonic() - BOOT_TS) * 1000)
    parts = [f"[{ms:07d}ms] {tag}"]
    for k, v in fields.items():
        try:
            s = json.dumps(v, separators=(",", ":")) if not isinstance(v, str) else v
        except Exception:
            s = str(v)
        parts.append(f"{k}={s}")
    print(" ".join(parts))

def b64preview(b64: str, n=16):
    if not b64:
        return "∅"
    return f"{len(b64)}:{b64[:n]}..."


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

        # --- Minimal GA session.update ---
        # Keep this VERY small to avoid 'unknown_parameter' issues.
        session_update = {
            "type": "session.update",
            "session": {
                # Ask for audio out; keep voice at the session level.
                "voice": OPENAI_VOICE or "alloy",
                "output_modalities": ["audio"],
                "audio": {
                    "input":  {"format": "audio/pcmu"},
                    "output": {"format": "audio/pcmu"}
                }
            }
        }
        loop.run_until_complete(openai_ws.send(json.dumps(session_update)))
        print("[stream] sent session.update (GA minimal, audio/pcmu)")

        # Start OpenAI->Twilio reader FIRST so we don't miss early deltas
        stream_info = {"sid": None}
        recv_task = loop.create_task(openai_to_twilio(ws, openai_ws, stream_info))
        loop.run_until_complete(asyncio.sleep(0))

        # Put a single user item that forces TTS generation
        greet_item = {
            "type": "conversation.item.create",
            "item": {
                "type": "message",
                "role": "user",
                "content": [
                    {"type": "input_text",
                     "text": "Say exactly this one sentence: Hello from Escallop."}
                ]
            }
        }
        loop.run_until_complete(openai_ws.send(json.dumps(greet_item)))
        print("[stream] sent conversation.item.create (test line)")

        # Trigger the model to respond (no response.modalities in GA)
        loop.run_until_complete(openai_ws.send(json.dumps({"type": "response.create"})))
        print("[stream] sent response.create (test)")

        # Now listen for Twilio events (captures streamSid from 'start')
        send_task = loop.create_task(twilio_to_openai(ws, openai_ws, stream_info))

        # Wait until one side ends
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
