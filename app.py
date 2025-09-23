# --- Imports ---
from flask import Flask, Response, request
from flask_sock import Sock
import os
import json
import base64
import threading
import time
import asyncio
import websockets
import audioop
from datetime import datetime
import uuid

DEBUG_HEARTBEAT_MS = int(os.getenv("DEBUG_HEARTBEAT_MS", "1000"))  # 1s
DEBUG_LOG_SAMPLES = int(os.getenv("DEBUG_LOG_SAMPLES", "0"))       # 0=off, >0 logs first N PCM16 samples

CALL_TRACE_ID = None  # set per call

def new_trace_id() -> str:
    return uuid.uuid4().hex[:8]

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

def rms_pcm16(buf: bytes) -> int:
    try:
        return audioop.rms(buf, 2)  # 16-bit width
    except Exception:
        return -1


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


# --- Safe default: Say-only webhook ---
@app.post("/voice")
def voice():
    twiml = """<?xml version="1.0" encoding="UTF-8"?>
<Response>
  <Say voice="Polly.Matthew">Streaming is not turned on yet. This is a test message to confirm the webhook works.</Say>
</Response>"""
    return Response(twiml, mimetype="text/xml")


@app.post("/voice_pcmout_test")
def voice_pcmout_test():
    base = (PUBLIC_BASE_URL or "https://escallop-voice-pm-aa62425200e7.herokuapp.com").rstrip("/")
    if base.startswith("https://"):
        stream_url = "wss://" + base[len("https://"):] + "/stream_pcmout"
    elif base.startswith("http://"):
        stream_url = "ws://" + base[len("http://"):] + "/stream_pcmout"
    else:
        stream_url = "wss://escallop-voice-pm-aa62425200e7.herokuapp.com/stream_pcmout"

    twiml = f"""<?xml version="1.0" encoding="UTF-8"?>
<Response>
  <Connect>
    <Stream url="{stream_url}"/>
  </Connect>
</Response>"""
    return Response(twiml, mimetype="text/xml")


# --- Streaming test webhook (TwiML) ---
@app.post("/voice_stream_test")
def voice_stream_test():
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
    model = OPENAI_REALTIME_MODEL or "gpt-realtime"
    url = f"wss://api.openai.com/v1/realtime?model={model}"
    headers = {"Authorization": f"Bearer {OPENAI_API_KEY}"}
    ws = await websockets.connect(
        url,
        extra_headers=headers,
        ping_interval=20,
        ping_timeout=20,
        max_size=None
    )
    return ws


async def openai_to_twilio(twilio_ws, openai_ws, stream_info):
    """
    Read OpenAI events. Forward audio if we get deltas.
    """
    text_chars = 0
    audio_frames = 0

    def send_ulaw_b64_to_twilio(b64audio: str):
        sid = stream_info.get("sid")
        if not sid:
            return
        try:
            raw = base64.b64decode(b64audio)
            payload = base64.b64encode(raw).decode("utf-8")
        except Exception:
            payload = b64audio
        try:
            twilio_ws.send(json.dumps({
                "event": "media",
                "streamSid": sid,
                "media": {"payload": payload}
            }))
        except Exception as e:
            print("[twilio<-openai] send error:", e)

    try:
        async for raw in openai_ws:
            try:
                evt = json.loads(raw)
            except Exception:
                continue

            t = evt.get("type")
            if not t:
                continue

            if t in ("response.created", "response.done", "session.updated", "session.created"):
                print("[openai.diag] event", json.dumps({"type": t}))

            if t in ("response.output_text.delta", "response.text.delta"):
                delta = evt.get("delta") or ""
                text_chars += len(delta)
                continue

            b64audio = None
            if t in ("response.audio.delta", "response.output_audio.delta"):
                b64audio = evt.get("delta")
            elif t in ("response.output_item.delta", "response.delta"):
                maybe = evt.get("delta")
                if isinstance(maybe, dict):
                    b64audio = maybe.get("audio")

            if b64audio:
                audio_frames += 1
                send_ulaw_b64_to_twilio(b64audio)
                continue

            if t == "error":
                print("[openai.diag] error", evt)

        sid = stream_info.get("sid")
        if sid:
            try:
                twilio_ws.send(json.dumps({"event": "stop", "streamSid": sid}))
            except Exception:
                pass
        print(f"[openai.summary] text_chars={text_chars} audio_frames={audio_frames}")
    except Exception as e:
        print("[openai_to_twilio] exception:", repr(e))


async def twilio_to_openai(twilio_ws, openai_ws, stream_info):
    """
    Forward Twilio μ-law frames to OpenAI.
    """
    frames = 0
    last_evt = None

    while True:
        try:
            msg = await asyncio.to_thread(twilio_ws.receive)
        except Exception as e:
            print(f"[twilio.recv.exception] {e}")
            break

        if msg is None:
            break

        try:
            data = json.loads(msg)
        except Exception:
            continue

        evt = data.get("event")
        if evt != last_evt:
            print(f"[twilio.event] {evt}")
            last_evt = evt

        if evt == "start":
            sid = data.get("start", {}).get("streamSid")
            stream_info["sid"] = sid
            print(f"[twilio.start] streamSid={sid}")
        elif evt == "media":
            payload = data.get("media", {}).get("payload")
            if payload:
                frames += 1
                if frames <= 3 or frames % 50 == 0:
                    print(f"[twilio.media.in] frames={frames}")
                try:
                    await openai_ws.send(json.dumps({
                        "type": "input_audio_buffer.append",
                        "audio": payload
                    }))
                except Exception as e:
                    print(f"[openai.append.error] {e}")
        elif evt == "stop":
            print("[twilio.stop] received")
            break

    return


# --- WebSocket: Twilio connects here ---
# --- WebSocket: Twilio connects here ---
@sock.route("/stream")
def stream(ws):
    trace_id = new_trace_id()
    print(f"[CALL {trace_id}] ---- Call started ----")

    if not OPENAI_API_KEY:
        try:
            ws.send(json.dumps({"event": "stop"}))
        except Exception:
            pass
        print(f"[CALL {trace_id}] No API key, ending immediately.")
        return

    loop = asyncio.new_event_loop()

    def runner():
        asyncio.set_event_loop(loop)
        try:
            openai_ws = loop.run_until_complete(openai_realtime_connect())
            print(f"[CALL {trace_id}] Connected to OpenAI realtime API.")

            session_update = {
                "type": "session.update",
                "session": {
                    "type": "realtime",
                    "model": OPENAI_REALTIME_MODEL or "gpt-realtime",
                    "output_modalities": ["audio"],
                    "audio": {
                        "input": {"format": {"type": "audio/pcmu"}},
                        "output": {"format": {"type": "audio/pcmu"},
                                   "voice": OPENAI_VOICE or "alloy"}
                    },
                    "instructions": "You are a voice agent. Speak replies out loud; keep them brief."
                }
            }
            loop.run_until_complete(openai_ws.send(json.dumps(session_update)))
            print(f"[CALL {trace_id}] Sent session.update to OpenAI.")

            stream_info = {"sid": None}

            recv_task = loop.create_task(openai_to_twilio(ws, openai_ws, stream_info))
            send_task = loop.create_task(twilio_to_openai(ws, openai_ws, stream_info))

            loop.run_until_complete(asyncio.gather(send_task, recv_task, return_exceptions=True))
            print(f"[CALL {trace_id}] Twilio ↔ OpenAI tasks finished.")

            loop.run_until_complete(openai_ws.close())
            print(f"[CALL {trace_id}] Closed OpenAI websocket.")
        except Exception as e:
            print(f"[CALL {trace_id}] ERROR in runner: {e!r}")

    t = threading.Thread(target=runner, daemon=True)
    t.start()

    try:
        while t.is_alive():
            time.sleep(0.05)
    except Exception as e:
        print(f"[CALL {trace_id}] ERROR waiting for thread: {e!r}")
    finally:
        print(f"[CALL {trace_id}] ---- Call ended ----")



# --- Main ---
if __name__ == "__main__":
    app.run(host="0.0.0.0", port=8000)
