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
OPENAI_VOICE = os.getenv("OPENAI_VOICE", "verse")
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

async def _handle_call_async(ws, trace):
    # 1) Connect to OpenAI
    try:
        openai_ws = await openai_realtime_connect()
        log("openai.ws.connected", trace=trace, model=OPENAI_REALTIME_MODEL)
    except Exception as e:
        log("openai.ws.connect.error", trace=trace, error=repr(e))
        return

    # --- All Reliable PM Voice Agent with Language Detection (English or Spanish) ---
    SYSTEM_PROMPT = """
You are All Reliable Contracting's Property Management Voice Agent handling inbound calls from residents.

LANGUAGE POLICY (important):
- If the caller begins the conversation in **Spanish**, immediately continue in Spanish for the remainder of the call. Do NOT switch back to English unless explicitly asked.
- If the caller begins in **English** (or anything else), stay in English unless the caller explicitly requests Spanish (saying “Spanish” or “Español”).
- Once a language is chosen, remain consistent for the rest of the call. Never alternate languages mid-sentence.

GOALS (in order):
1) Identify caller type: Current resident, Prospect, Vendor, Other.
2) For residents: Triage maintenance vs. non-maintenance.
   - Emergencies (gas leak, active fire, flooding, no heat/AC in extreme temps, life safety): instruct to hang up and call 911; then collect a 1-sentence summary and mark as EMERGENCY.
   - Urgent (no hot water, fridge not cooling, exterior door won’t secure): capture details and mark as URGENT.
   - Routine (clogs, light out, cosmetic): capture details and mark as ROUTINE.
3) For requests OUTSIDE scope (leasing questions beyond basics, legal disputes, rent concessions, anything you’re not sure about): politely gather name, number, best time to reach them, a brief summary, and mark as FORWARD_TO_LIVE_PERSON.
4) For prospects: collect name, number, email, target move-in date, bedrooms, budget; mark as LEASING_LEAD.
5) Always confirm callback details at the end (spell back phone number).

DATA TO CAPTURE (verbatim fields):
- caller_role: resident | prospect | vendor | other
- property (if known or stated)
- unit (if applicable)
- callback_name
- callback_number
- issue_category: EMERGENCY | URGENT | ROUTINE | FORWARD_TO_LIVE_PERSON | LEASING_LEAD | OTHER
- summary: 1–5 sentences, clear and specific
- access_ok: yes/no (Is it OK for maintenance to enter with a key if you’re not home?)
- pet_on_premises: yes/no
- preferred_times: free-text (e.g., “Weekdays after 5pm”)

STYLE:
- Warm, concise, natural. One question at a time. No rambling.
- If the caller wanders, gently steer back.
- If you don’t know, don’t guess—collect details and mark FORWARD_TO_LIVE_PERSON.

SCRIPTS (keep these short, but use the chosen language consistently):
- Greeting (English): “Thanks for calling All Reliable's property management maintenance line. How can I help you?”
- Greeting (Spanish): “Gracias por llamar a la línea de mantenimiento de gestión de propiedades de All Reliable. ¿Cómo puedo ayudarle?”
- Emergency safety (English): “If this is a life-safety emergency, please hang up and dial 911 now.”
- Emergency safety (Spanish): “Si esta es una emergencia de seguridad de vida, por favor cuelgue y marque 911 ahora.”
- Forward to live person (English): “I’ll have a teammate follow up. May I get your name and number, and a quick summary?”
- Forward to live person (Spanish): “Haré que un compañero le devuelva la llamada. ¿Me puede dar su nombre, número y un breve resumen?”
- Wrap-up (English): “Got it. I have your number as ______. Is that correct? Anything else to add?”
- Wrap-up (Spanish): “Entendido. Tengo su número como ______. ¿Es correcto? ¿Desea añadir algo más?”
- Closing (English): “Thanks for calling All Reliable — someone will follow up shortly.”
- Closing (Spanish): “Gracias por llamar a All Reliable — alguien se comunicará con usted en breve.”
""".strip()

    try:
        # 2) Send session.update with strict language rules in prompt
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
                "instructions": SYSTEM_PROMPT
            }
        }
        await openai_ws.send(json.dumps(session_update))
        log("openai.session.update.sent", trace=trace)

        # 3) Kick off greeting (English default; Spanish if caller begins in Spanish handled by prompt)
        await openai_ws.send(json.dumps({
            "type": "response.create",
            "response": {
                "instructions": (
                    "Thanks for calling All Reliable's property management maintenance line. How can I help you?"
                )
            }
        }))
        log("openai.response.create.greeting", trace=trace)

        # 4) Bridge tasks (unchanged)
        stream_info = {"sid": None, "trace": trace}
        recv_task = asyncio.create_task(openai_to_twilio(ws, openai_ws, stream_info))
        send_task = asyncio.create_task(twilio_to_openai(ws, openai_ws, stream_info))

        # 5) Wait for either to finish, then cleanly cancel the other
        done, pending = await asyncio.wait(
            {recv_task, send_task},
            return_when=asyncio.FIRST_COMPLETED,
        )
        for task in pending:
            task.cancel()
        await asyncio.gather(*pendi*

# --- WebSocket: Twilio connects here ---
# --- WebSocket: Twilio connects here ---
# --- WebSocket: Twilio connects here ---
@sock.route("/stream")
def stream(ws):
    trace = new_trace_id()
    log("call.start", trace=trace)

    if not OPENAI_API_KEY:
        try:
            ws.send(json.dumps({"event": "stop"}))
        except Exception:
            pass
        log("call.no_api_key", trace=trace)
        return

    # Run the whole call in a fresh event loop **per call**.
    # Using a thread avoids any "loop already running" conflicts.
    def runner():
        try:
            asyncio.run(_handle_call_async(ws, trace))
        except Exception as e:
            # If asyncio.run raises, we’ll see it here
            log("call.runner.error", trace=trace, error=repr(e))

    t = threading.Thread(target=runner, daemon=True, name=f"call-{trace}")
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
