# --- Imports ---
import os
import json
import time
import threading
import asyncio
import base64
from datetime import datetime

from flask import Flask, Response, Request
from flask_sock import Sock
import websockets

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
    return {"status": "ok", "ts": datetime.utcnow().isoformat() + "Z"}

# --- Simple “safe” webhook (does not stream) ---
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
    """
    Use this as your Twilio Voice webhook while testing.
    It opens a WebSocket back to /stream for live audio in/out.
    """
    base = (PUBLIC_BASE_URL or "https://escallop-voice-pm-aa62425200e7.herokuapp.com").rstrip("/")
    if base.startswith("https://"):
        stream_url = "wss://" + base[len("https://"):] + "/stream"
    elif base.startswith("http://"):
        stream_url = "ws://" + base[len("http://"):] + "/stream"
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
    Connect to OpenAI Realtime WebSocket (v1).
    """
    model = OPENAI_REALTIME_MODEL or "gpt-4o-realtime-preview"
    url = f"wss://api.openai.com/v1/realtime?model={model}"
    headers = {
        "Authorization": f"Bearer {OPENAI_API_KEY}",
        "OpenAI-Beta": "realtime=v1",
    }
    ws = await websockets.connect(
        url,
        extra_headers=headers,
        ping_interval=20,
        ping_timeout=20,
        max_size=None
    )
    return ws


async def twilio_to_openai(twilio_ws, openai_ws, stream_info):
    """
    Twilio -> OpenAI
    Forward Twilio μ-law (audio/pcmu @ 8kHz) frames directly to OpenAI.
    We append base64 payloads as 'audio' strings.
    Server VAD on the OpenAI side handles commits, so we do not commit manually.
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
                # Light logging so we know frames are flowing
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
    OpenAI -> Twilio
    - Logs event types for debugging.
    - Forwards audio deltas to Twilio as 'media' events (μ-law 8k base64).
    - Also logs text deltas so we can see if the model is replying text-only.
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
        # Normalize base64 (defensive)
        try:
            payload = base64.b64encode(base64.b64decode(b64audio)).decode("utf-8")
        except Exception:
            payload = b64audio
        try:
            twilio_ws.send(json.dumps({"event": "media", "streamSid": sid, "media": {"payload": payload}}))
        except Exception as e:
            log_event("twilio.media.error", err=str(e))
            return
        audio_frames += 1
        if audio_frames in (1, 10, 50) or audio_frames % 50 == 0:
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

            # AUDIO DELTAS (server sends base64 audio — with our session set to g711_ulaw)
            b64audio = None
            if t in ("response.output_audio.delta", "response.audio.delta"):
                b64audio = evt.get("delta")
                if isinstance(b64audio, dict):  # sometimes {"audio": "..."}
                    b64audio = b64audio.get("audio")
            elif t in ("response.output_item.delta", "response.delta"):
                maybe = evt.get("delta")
                if isinstance(maybe, dict):
                    b64audio = maybe.get("audio")
            elif isinstance(evt.get("data"), dict):
                b64audio = evt["data"].get("audio")

            if b64audio:
                send_to_twilio_pc_mu(b64audio)

            # TEXT DELTAS (handy to confirm the model is responding)
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
            twilio_ws.send(json.dumps({"event": "stop", "streamSid": sid}))
        except Exception:
            pass


# --- WebSocket: Twilio connects here ---
@sock.route("/stream")
def stream(ws):
    """
    Primary media-stream bridge for Twilio <-> OpenAI.
    - Twilio sends μ-law 8k audio frames to us over this WebSocket.
    - We forward those frames to OpenAI.
    - We forward OpenAI audio deltas back to Twilio as 'media' events.
    """
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

        # ---- Configure session for PHONE AUDIO (μ-law 8k in/out) ----
        session_update = {
            "type": "session.update",
            "session": {
                # Audio + text is fine; audio is what matters for phone
                "modalities": ["audio", "text"],
                # Tell OpenAI what we're sending and what we want back
                "input_audio_format": "g711_ulaw",
                "output_audio_format": "g711_ulaw",
                "voice": OPENAI_VOICE or "alloy",
                "instructions": (
                    "You are a helpful property management agent. "
                    "Speak out loud, be concise, and acknowledge the caller’s intent."
                )
            }
        }
        loop.run_until_complete(openai_ws.send(json.dumps(session_update)))
        print("[stream] session.update sent (g711_ulaw in/out, audio+text)")

        # Start OpenAI -> Twilio reader first so we never miss audio deltas
        stream_info = {"sid": None}
        recv_task = loop.create_task(openai_to_twilio(ws, openai_ws, stream_info))
        loop.run_until_complete(asyncio.sleep(0))

        # Twilio -> OpenAI (append μ-law frames; NO manual commit — server VAD handles it)
        send_task = loop.create_task(twilio_to_openai(ws, openai_ws, stream_info))

        # ✅ Wait briefly for Twilio streamSid before asking model to speak
        async def wait_for_sid_then_greet():
            for _ in range(100):  # ~2s total
                if stream_info.get("sid"):
                    break
                await asyncio.sleep(0.02)
            # Ask model to speak (no response.modalities — session already set)
            await openai_ws.send(json.dumps({
                "type": "response.create",
                "response": {
                    "instructions": "Say exactly: Hello from Escallop."
                }
            }))
            print("[stream] response.create sent")

        kickoff_task = loop.create_task(wait_for_sid_then_greet())

        loop.run_until_complete(asyncio.gather(send_task, recv_task, kickoff_task, return_exceptions=True))

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
    # For local testing; on Heroku, Gunicorn will serve the app.
    app.run(host="0.0.0.0", port=8000)
