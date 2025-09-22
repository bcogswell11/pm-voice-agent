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
    DIAGNOSTIC VERSION:
    - Logs every OpenAI event type (first 20, then every 20th)
    - Logs full error payloads
    - Logs text deltas (so we can see if the model is replying in text-only)
    - Forwards audio deltas to Twilio (and logs every 1st/10th/50th media send)
    """
    event_count = 0
    audio_frames = 0
    text_chunks = 0

    def log_event(tag, **kw):
        try:
            print(f"[openai.diag] {tag} " + json.dumps(kw))
        except Exception:
            print(f"[openai.diag] {tag} {kw}")

    def send_to_twilio_pc_mu(b64audio: str):
        nonlocal audio_frames
        sid = stream_info.get("sid")
        if not sid:
            log_event("twilio.media.skip", reason="no_streamSid_yet")
            return
        # normalize base64 to be safe
        try:
            payload = base64.b64encode(base64.b64decode(b64audio)).decode("utf-8")
        except Exception:
            payload = b64audio
        twilio_ws.send(json.dumps({"event":"media","streamSid":sid,"media":{"payload":payload}}))
        audio_frames += 1
        if audio_frames in (1,10,50) or audio_frames % 50 == 0:
            log_event("twilio.media.sent", count=audio_frames, payload_preview=(payload[:24] + "..."))

    try:
        async for raw in openai_ws:
            event_count += 1
            try:
                evt = json.loads(raw)
            except Exception:
                log_event("event.parse_error", raw_len=len(raw))
                continue

            t = evt.get("type") or "?"
            if event_count <= 20 or event_count % 20 == 0:
                log_event("event", type=t)

            if t == "error":
                log_event("error", raw=evt)  # full payload
                continue

            # AUDIO DELTAS (pcmu or pcm—server still sends base64 bytes)
            b64audio = None
            if t in ("response.output_audio.delta", "response.audio.delta"):
                b64audio = evt.get("delta")
                if isinstance(b64audio, dict):  # some shapes: {"audio": "..."}
                    b64audio = b64audio.get("audio")
            elif t in ("response.output_item.delta", "response.delta"):
                maybe = evt.get("delta")
                if isinstance(maybe, dict):
                    b64audio = maybe.get("audio")
            elif isinstance(evt.get("data"), dict):
                b64audio = evt["data"].get("audio")

            if b64audio:
                send_to_twilio_pc_mu(b64audio)

            # TEXT DELTAS (to detect text-only replies)
            if t in ("response.output_text.delta", "response.text.delta"):
                delta = evt.get("delta")
                if isinstance(delta, dict):
                    delta = delta.get("text", "")
                if isinstance(delta, str) and delta:
                    text_chunks += 1
                    if text_chunks <= 5 or text_chunks % 10 == 0:
                        log_event("text.delta", preview=(delta[:80] + ("..." if len(delta) > 80 else "")))

            if t in ("response.completed", "response.done"):
                log_event("response.summary", audio_frames=audio_frames, text_chunks=text_chunks, total_events=event_count)
    except Exception as e:
        log_event("reader.exception", err=str(e))

    # graceful stop
    sid = stream_info.get("sid")
    if sid:
        try:
            twilio_ws.send(json.dumps({"event":"stop","streamSid":sid}))
        except Exception:
            pass




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
        print("[stream] openai connected")

        # PCMU in/out, audio-only, server VAD ON
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
        loop.run_until_complete(openai_ws.send(json.dumps(session_update)))
        print("[stream] session.update sent (pcmu in/out, audio-only)")

        # Start OpenAI -> Twilio reader first so we don’t miss audio deltas
        stream_info = {"sid": None}
        recv_task = loop.create_task(openai_to_twilio(ws, openai_ws, stream_info))
        loop.run_until_complete(asyncio.sleep(0))

        # ✅ Correct schema: response.create with a nested 'response' object
        loop.run_until_complete(openai_ws.send(json.dumps({
            "type": "response.create",
            "response": {
                "instructions": "Say exactly: Hello from Escallop."
                # (no modalities here; session is audio-only already)
            }
        })))
        print("[stream] response.create (nested response.instructions) sent")

        # Twilio -> OpenAI (append PCMU frames; NO manual commit — server VAD handles it)
        send_task = loop.create_task(twilio_to_openai(ws, openai_ws, stream_info))

        loop.run_until_complete(asyncio.gather(send_task, recv_task, return_exceptions=True))

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
