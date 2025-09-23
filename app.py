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
import uuid

DEBUG_HEARTBEAT_MS = int(os.getenv("DEBUG_HEARTBEAT_MS", "1000"))  # 1s
DEBUG_LOG_SAMPLES  = int(os.getenv("DEBUG_LOG_SAMPLES",  "0"))     # 0=off, >0 logs first N PCM16 samples

CALL_TRACE_ID = None  # set per call

def new_trace_id() -> str:
    return uuid.uuid4().hex[:8]

def log(tag, **fields):
    ms = int((time.monotonic() - BOOT_TS) * 1000)
    base = {"ms": ms, "trace": CALL_TRACE_ID or "na", "tag": tag}
    base.update(fields)
    try:
        print(json.dumps(base, separators=(",", ":"), ensure_ascii=False))
    except Exception:
        print(f"[{ms:07d}ms] {tag} " + " ".join(f"{k}={v}" for k,v in fields.items()))

def b64preview(b64: str, n=16):
    if not b64: return "∅"
    return f"{len(b64)}:{b64[:n]}..."

def rms_pcm16(buf: bytes) -> int:
    try:
        return audioop.rms(buf, 2)  # 16-bit width
    except Exception:
        return -1

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
    Forward Twilio μ-law (audio/pcmu @ 8kHz) frames directly to OpenAI:
      - append base64 payload as 'audio' (string)
      - DO NOT call input_audio_buffer.commit; server_vad will commit
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
            print("[twilio] ws.receive() returned None")
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
            print(f"[twilio.start] streamSid={sid} tracks={data.get('start',{}).get('tracks')}")
        elif evt == "media":
            payload = data.get("media", {}).get("payload")
            if payload:
                frames += 1
                # Optional: log a little to know frames are flowing
                if frames <= 3 or frames % 50 == 0:
                    print(f"[twilio.media.in] frames={frames}")
                try:
                    await openai_ws.send(json.dumps({
                        "type": "input_audio_buffer.append",
                        "audio": payload  # base64 μ-law bytes
                    }))
                except Exception as e:
                    print(f"[openai.append.error] {e}")
        elif evt == "stop":
            print("[twilio.stop] received")
            break
        # ignore other events

    return

async def openai_to_twilio(twilio_ws, openai_ws, stream_info):
    """
    Read OpenAI events. Forward audio to Twilio without blocking the event loop.
    - Buffers audio deltas until Twilio sends us a streamSid.
    - Uses asyncio.to_thread() for twilio_ws.send() which is blocking.
    - Logs text deltas (handy when you enable text output later).
    """
    text_chars = 0
    audio_frames = 0
    pending_ulaw = []  # holds base64 μ-law chunks until streamSid exists

    async def safe_twilio_send(payload: str):
        # Offload blocking .send() to a thread so the event loop isn't blocked.
        def _send():
            twilio_ws.send(payload)
        try:
            await asyncio.to_thread(_send)
        except Exception as e:
            print("[twilio<-openai] send error:", e)

    async def flush_buffer_if_ready():
        sid = stream_info.get("sid")
        if not sid or not pending_ulaw:
            return
        # gentle pacing so Twilio plays smoothly
        while pending_ulaw:
            b64audio = pending_ulaw.pop(0)
            msg = json.dumps({
                "event": "media",
                "streamSid": sid,
                "media": {"payload": b64audio}
            })
            await safe_twilio_send(msg)
            await asyncio.sleep(0.02)

    try:
        async for raw in openai_ws:
            try:
                evt = json.loads(raw)
            except Exception:
                continue

            t = evt.get("type")
            if not t:
                continue

            # --- DIAGNOSTICS ---
            if t in ("response.created", "response.done", "session.updated", "session.created"):
                print("[openai.diag] event", json.dumps({"type": t}))

            # text stream?
            if t in ("response.output_text.delta", "response.text.delta"):
                delta = evt.get("delta") or ""
                text_chars += len(delta)
                if text_chars <= 200:
                    print(f"[openai.text] +{len(delta)} chars (total={text_chars})")
                continue

            # audio stream? (common GA shapes)
            b64audio = None
            if t in ("response.audio.delta", "response.output_audio.delta"):
                b64audio = evt.get("delta")
            elif t in ("response.output_item.delta", "response.delta"):
                maybe = evt.get("delta")
                if isinstance(maybe, dict):
                    b64audio = maybe.get("audio")

            if b64audio:
                audio_frames += 1
                # enqueue until we have a Twilio streamSid
                pending_ulaw.append(b64audio)
                await flush_buffer_if_ready()
                continue

            if t == "error":
                print("[openai.diag] error", json.dumps({"raw": evt}))
                continue

        # stream closed: try to stop Twilio stream
        sid = stream_info.get("sid")
        if sid:
            await safe_twilio_send(json.dumps({"event": "stop", "streamSid": sid}))
        print(f"[openai.summary] text_chars={text_chars} audio_frames={audio_frames}")

    except Exception as e:
        print("[openai_to_twilio] exception:", repr(e))


# --- WebSocket: Twilio connects here ---
@sock.route("/stream")
def stream(ws):
    if not OPENAI_API_KEY:
        try:
            ws.send(json.dumps({"event": "stop"}))
        except Exception:
            pass
        return

    def thread_target():
        async def async_runner():
            # 1) Connect to OpenAI Realtime WS
            openai_ws = await openai_realtime_connect()
            print("[stream] openai connected")

            # 2) Apply session: PCMU in/out, audio-only (keep as-is)
            session_update = {
                "type": "session.update",
                "session": {
                    "type": "realtime",
                    "model": OPENAI_REALTIME_MODEL or "gpt-realtime",
                    "output_modalities": ["audio"],
                    "audio": {
                        "input": {
                            "format": {"type": "audio/pcmu"},
                            "turn_detection": {"type": "server_vad", "silence_duration_ms": 500}
                        },
                        "output": {
                            "format": {"type": "audio/pcmu"},
                            "voice": OPENAI_VOICE or "alloy"
                        }
                    },
                    "instructions": "You are a voice agent. Speak replies out loud; keep them brief."
                }
            }
            await openai_ws.send(json.dumps(session_update))
            print("[stream] session.update sent (pcmu in/out, audio-only)")

            # 3) Start OpenAI -> Twilio reader FIRST so we don't miss early audio deltas
            stream_info = {"sid": None}
            recv_task = asyncio.create_task(openai_to_twilio(ws, openai_ws, stream_info))
            await asyncio.sleep(0)  # yield to start the task

            # 4) Small delay to let session apply, then force a spoken greeting
            await asyncio.sleep(0.15)
            await openai_ws.send(json.dumps({
                "type": "response.create",
                "response": {
                    "instructions": "Say exactly: Hello from Escallop."
                }
            }))
            print("[stream] response.create sent (instructions only)")

            # 5) Start Twilio -> OpenAI sender (server VAD will commit turns)
            send_task = asyncio.create_task(twilio_to_openai(ws, openai_ws, stream_info))

            # 6) Run both until done
            try:
                await asyncio.gather(send_task, recv_task)
            finally:
                try:
                    await openai_ws.close()
                except Exception:
                    pass

        # Create/close a fresh event loop for this call (prevents nested-loop errors)
        try:
            asyncio.run(async_runner())
        except Exception as e:
            print("[stream.thread] exception:", repr(e))

    t = threading.Thread(target=thread_target, daemon=True)
    t.start()
    try:
        while t.is_alive():
            time.sleep(0.05)
    except Exception:
        pass


# --- Main (local only) ---
if __name__ == "__main__":
    app.run(host="0.0.0.0", port=8000)
