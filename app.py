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
    Twilio -> OpenAI (server VAD flow)
    - Capture streamSid
    - For each Twilio 'media' event, forward the base64 payload as-is
      to OpenAI using input_audio_buffer.append.
    - Do NOT call input_audio_buffer.commit. With server_vad, the server
      commits automatically and will emit input_audio_buffer.speech_started /
      .speech_stopped / .committed events.
    """
    frames = 0
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
            stream_info["sid"] = data.get("start", {}).get("streamSid")
            print(f"[twilio] start streamSid={stream_info['sid']} track={data.get('start',{}).get('tracks')}")
        elif evt == "media":
            payload = data.get("media", {}).get("payload")
            if payload:
                try:
                    await openai_ws.send(json.dumps({
                        "type": "input_audio_buffer.append",
                        "audio": payload  # base64 audio/pcmu bytes
                    }))
                    frames += 1
                    if frames % 20 == 0:
                        print(f"[openai.append] frames={frames}")
                except Exception as e:
                    print(f"[openai.append] send error: {e}")
        elif evt == "stop":
            print("[twilio] stop")
            break
        # ignore other events



async def openai_to_twilio(twilio_ws, openai_ws, stream_info):
    """
    Reads OpenAI events; forwards audio deltas to Twilio.
    NOW logs full error payloads and previews text deltas so we can see
    if the model is silently choosing text-only or erroring out.
    """
    counts = {}
    tw_media_sent = 0
    WIDTH = 2
    CH = 1
    IN_RATE = 16000
    OUT_RATE = 8000

    def bump(t): counts[t] = counts.get(t, 0) + 1

    def pcm16_16k_to_ulaw8k(b64_pcm: str) -> str:
        try:
            pcm16 = base64.b64decode(b64_pcm)
            if not pcm16:
                return ""
            pcm8k, _ = audioop.ratecv(pcm16, WIDTH, CH, IN_RATE, OUT_RATE, None)
            ulaw = audioop.lin2ulaw(pcm8k, WIDTH)
            return base64.b64encode(ulaw).decode("utf-8")
        except Exception:
            return ""

    def send_to_twilio_from_pcm(b64_pcm: str):
        nonlocal tw_media_sent
        sid = stream_info.get("sid")
        if not sid:
            log("twilio.media.skip", reason="no_streamSid_yet"); return
        b64_ulaw = pcm16_16k_to_ulaw8k(b64_pcm)
        if not b64_ulaw:
            log("pcm_to_ulaw.empty"); return
        try:
            twilio_ws.send(json.dumps({"event":"media","streamSid":sid,"media":{"payload":b64_ulaw}}))
            tw_media_sent += 1
            if tw_media_sent <= 3 or tw_media_sent % 20 == 0:
                log("twilio.media.sent", count=tw_media_sent, payload_preview=b64preview(b64_ulaw))
        except Exception as e:
            log("twilio.media.error", err=str(e))

    try:
        async for raw in openai_ws:
            try:
                evt = json.loads(raw)
            except Exception:
                log("openai.event.parse_error", raw_len=len(raw)); continue

            t = evt.get("type") or "?"
            bump(t)

            # 🔎 Log full error payloads so we can see the exact reason
            if t == "error":
                log("openai.error", raw=evt)   # <-- key addition
                continue

            # keep an eye on event mix
            if sum(counts.values()) <= 10 or sum(counts.values()) % 50 == 0:
                log("openai.event", type=t)

            # Handle audio deltas (PCM16@16k)
            b64audio = None
            if t in ("response.output_audio.delta", "response.audio.delta"):
                b64audio = evt.get("delta")
                if isinstance(b64audio, dict):
                    b64audio = b64audio.get("audio")
            elif t in ("response.output_item.delta", "response.delta"):
                maybe = evt.get("delta")
                if isinstance(maybe, dict):
                    b64audio = maybe.get("audio")
            elif isinstance(evt.get("data"), dict):
                b64audio = evt["data"].get("audio")

            if b64audio:
                send_to_twilio_from_pcm(b64audio)

            # 👀 Also log text deltas to see if the model is replying in text-only
            if t in ("response.output_text.delta", "response.text.delta"):
                txt = evt.get("delta") or ""
                if isinstance(txt, dict):
                    txt = txt.get("text", "")
                if isinstance(txt, str) and txt:
                    # log first few and every 20th chunk
                    if counts[t] <= 3 or counts[t] % 20 == 0:
                        log("openai.text.delta", preview=(txt[:60] + "..." if len(txt) > 60 else txt))

            if t in ("response.completed", "response.done"):
                log("openai.response.done_summary",
                    audio_frames=(counts.get("response.output_audio.delta", 0)
                                  + counts.get("response.audio.delta", 0)),
                    text_chunks=(counts.get("response.output_text.delta", 0)
                                  + counts.get("response.text.delta", 0)),
                    total_events=sum(counts.values()))
    except Exception as e:
        log("openai.reader.exception", err=str(e))

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
    global CALL_TRACE_ID
    CALL_TRACE_ID = new_trace_id()

    if not OPENAI_API_KEY:
        try: ws.send(json.dumps({"event": "stop"}))
        except Exception: pass
        return

    loop = asyncio.new_event_loop()

    async def heartbeat():
        while True:
            log("hb")
            await asyncio.sleep(DEBUG_HEARTBEAT_MS / 1000.0)

    def runner():
        asyncio.set_event_loop(loop)
        openai_ws = loop.run_until_complete(openai_realtime_connect())
        log("openai.connect.ok", model=OPENAI_REALTIME_MODEL or "gpt-realtime")

        # GA session.update:
        # - INPUT: PCM16 @16k (we already transcode μ-law -> pcm16 in twilio_to_openai)
        # - OUTPUT: PCM16 @16k (we will transcode -> μ-law for Twilio in openai_to_twilio)
        session_update = {
            "type": "session.update",
            "session": {
                "type": "realtime",
                "model": OPENAI_REALTIME_MODEL or "gpt-realtime",
                "output_modalities": ["audio","text"],
                "audio": {
                    "input": {
                        "format": {"type": "audio/pcm", "sample_rate_hz": 16000},
                        "turn_detection": {"type": "server_vad", "silence_duration_ms": 500}
                    },
                    "output": {
                        "format": {"type": "audio/pcm", "sample_rate_hz": 16000},
                        "voice": (OPENAI_VOICE or "alloy")
                    }
                },
                "instructions": "You are a voice agent. Speak concise replies aloud."
            }
        }
        loop.run_until_complete(openai_ws.send(json.dumps(session_update)))
        log("session.update.sent",
            voice=OPENAI_VOICE or "alloy",
            audio_in="pcm16@16kHz",
            audio_out="pcm16@16kHz",
            modalities=["audio","text"])

        stream_info = {"sid": None}
        recv_task = loop.create_task(openai_to_twilio(ws, openai_ws, stream_info))
        send_task = loop.create_task(twilio_to_openai(ws, openai_ws, stream_info))
        hb_task   = loop.create_task(heartbeat())

        loop.run_until_complete(asyncio.gather(send_task, recv_task, hb_task, return_exceptions=True))
        try: loop.run_until_complete(openai_ws.close())
        except Exception: pass

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
