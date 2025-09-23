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

def _bool(v):  # helper for clean booleans
    return bool(v and str(v).strip())

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
    return {
        "ok": True,
        "uptime_ms": int((time.monotonic() - BOOT_TS) * 1000),
        "service": "escallop-voice-pm",
        "ts": datetime.utcnow().isoformat() + "Z"
    }

@app.get("/ready")
def ready():
    checks = {
        "OPENAI_API_KEY_present": _bool(OPENAI_API_KEY),
        "OPENAI_REALTIME_MODEL_present": _bool(OPENAI_REALTIME_MODEL),
        "PUBLIC_BASE_URL_present": _bool(PUBLIC_BASE_URL),
    }
    ok = all(checks.values())
    return {
        "ok": ok,
        "checks": checks,
        "ws_stream_url": (
            ("wss://" + PUBLIC_BASE_URL[len("https://"):] + "/stream")
            if PUBLIC_BASE_URL.startswith("https://") else
            (("ws://" + PUBLIC_BASE_URL[len("http://"):] + "/stream")
             if PUBLIC_BASE_URL.startswith("http://") else
             "wss://escallop-voice-pm-aa62425200e7.herokuapp.com/stream")
        ),
        "voice_webhook": f"{(PUBLIC_BASE_URL or 'https://escallop-voice-pm-aa62425200e7.herokuapp.com').rstrip('/')}/voice_stream_test",
        "ts": datetime.utcnow().isoformat() + "Z"
    }, (200 if ok else 503)

@app.get("/version")
def version():
    return {
        "name": "escallop-voice-pm",
        "model": OPENAI_REALTIME_MODEL,
        "voice": OPENAI_VOICE,
        "public_base_url": PUBLIC_BASE_URL or "(default heroku app URL)",
        "python": os.getenv("PYTHON_VERSION", "3.x"),
        "stack_note": "If you see runtime.txt deprecation, switch to .python-version on Heroku"
    }

@app.get("/whoami")
def whoami():
    base = (PUBLIC_BASE_URL or "https://escallop-voice-pm-aa62425200e7.herokuapp.com").rstrip("/")
    return {
        "public_base_url": base,
        "twilio_stream_ws": (
            "wss://" + base[len("https://"):] + "/stream" if base.startswith("https://")
            else ("ws://" + base[len("http://"):] + "/stream" if base.startswith("http://")
                  else "wss://escallop-voice-pm-aa62425200e7.herokuapp.com/stream")
        ),
        "health_url": base + "/health",
        "ready_url": base + "/ready",
        "voice_webhook_for_testing": base + "/voice_stream_test"
    }

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
    Read OpenAI events and forward audio (unchanged behavior).
    Adds instrumentation so we can see if audio was produced and sent.
    """
    trace = stream_info.get("trace") or new_trace_id()
    text_chars = 0
    audio_frames = 0
    errors = 0

    def send_ulaw_b64_to_twilio(b64audio: str):
        sid = stream_info.get("sid")
        if not sid:
            # We can't send audio until Twilio sends 'start' with streamSid.
            if audio_frames <= 3:
                print(f"[{trace}] WARN: got OpenAI audio before Twilio 'start' (no streamSid yet)")
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
            print(f"[{trace}] ERROR twilio send: {e!r}")

    print(f"[{trace}] openai.pipe.start")
    try:
        async for raw in openai_ws:
            try:
                evt = json.loads(raw)
            except Exception:
                continue

            t = evt.get("type") or ""

            if t in ("response.created", "response.done", "session.updated", "session.created"):
                print(f"[{trace}] openai.event {t}")

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
                if audio_frames <= 3 or audio_frames % 50 == 0:
                    print(f"[{trace}] openai.audio.delta frames={audio_frames}")
                send_ulaw_b64_to_twilio(b64audio)
                continue

            if t == "error":
                errors += 1
                print(f"[{trace}] openai.error {evt}")

        sid = stream_info.get("sid")
        if sid:
            try:
                twilio_ws.send(json.dumps({"event": "stop", "streamSid": sid}))
            except Exception:
                pass
        print(f"[{trace}] openai.pipe.stop text_chars={text_chars} audio_frames={audio_frames} errors={errors}")
    except Exception as e:
        print(f"[{trace}] openai_to_twilio.exception {e!r}")


async def twilio_to_openai(twilio_ws, openai_ws, stream_info):
    """
    Forward Twilio μ-law frames to OpenAI (unchanged behavior).
    Adds instrumentation to prove whether Twilio sent 'start'/'media'.
    """
    trace = stream_info.get("trace") or new_trace_id()
    frames = 0
    last_evt = None
    got_start = False

    print(f"[{trace}] twilio.pipe.start")

    while True:
        try:
            msg = await asyncio.to_thread(twilio_ws.receive)
        except Exception as e:
            print(f"[{trace}] twilio.recv.exception {e!r}")
            break

        if msg is None:
            break

        try:
            data = json.loads(msg)
        except Exception:
            continue

        evt = data.get("event")
        if evt != last_evt:
            print(f"[{trace}] twilio.event {evt}")
            last_evt = evt

        if evt == "start":
            got_start = True
            sid = data.get("start", {}).get("streamSid")
            stream_info["sid"] = sid
            print(f"[{trace}] twilio.start streamSid={sid}")

        elif evt == "media":
            payload = data.get("media", {}).get("payload")
            if payload:
                frames += 1
                if frames <= 3 or frames % 50 == 0:
                    print(f"[{trace}] twilio.media.in frames={frames}")
                try:
                    await openai_ws.send(json.dumps({
                        "type": "input_audio_buffer.append",
                        "audio": payload
                    }))
                except Exception as e:
                    print(f"[{trace}] openai.append.error {e!r}")

        elif evt == "stop":
            print(f"[{trace}] twilio.stop")
            break

    print(f"[{trace}] twilio.pipe.stop got_start={got_start} total_frames={frames}")
    return




# --- WebSocket: Twilio connects here ---
# --- WebSocket: Twilio connects here ---
@sock.route("/stream")
def stream(ws):
    # Per-call trace
    trace = new_trace_id()
    log("call.start", trace=trace)

    if not OPENAI_API_KEY:
        try:
            ws.send(json.dumps({"event": "stop"}))
        except Exception:
            pass
        log("call.no_api_key", trace=trace)
        return

    loop = asyncio.new_event_loop()

    def runner():
        asyncio.set_event_loop(loop)
        try:
            openai_ws = loop.run_until_complete(openai_realtime_connect())
            log("openai.ws.connected", trace=trace, model=OPENAI_REALTIME_MODEL)

            # Keep your original session/update exactly as before (audio path unchanged)
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
            log("openai.session.update.sent", trace=trace)

            stream_info = {"sid": None, "trace": trace}

            recv_task = loop.create_task(openai_to_twilio(ws, openai_ws, stream_info))
            loop.run_until_complete(asyncio.sleep(0))  # yield

            # Keep your greeting so you can hear downlink immediately
            loop.run_until_complete(openai_ws.send(json.dumps({
                "type": "response.create",
                "response": {"instructions": "In English only, begin the conversation by saying exactly: Hello from Escallop."}
            })))
            log("openai.response.create.greeting", trace=trace)

            send_task = loop.create_task(twilio_to_openai(ws, openai_ws, stream_info))

            loop.run_until_complete(asyncio.gather(send_task, recv_task, return_exceptions=True))
            log("call.tasks.joined", trace=trace)

            try:
                loop.run_until_complete(openai_ws.close())
                log("openai.ws.closed", trace=trace)
            except Exception as e:
                log("openai.ws.close.error", trace=trace, error=repr(e))

        except Exception as e:
            log("call.runner.error", trace=trace, error=repr(e))

    t = threading.Thread(target=runner, daemon=True)
    t.start()
    try:
        while t.is_alive():
            time.sleep(0.05)
    except Exception as e:
        log("call.thread.wait.error", trace=trace, error=repr(e))
    finally:
        log("call.end", trace=trace)




# --- Main ---
if __name__ == "__main__":
    app.run(host="0.0.0.0", port=8000)
