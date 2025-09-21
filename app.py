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
    Transcode Twilio μ-law 8k -> PCM16 16k, append to OpenAI.
    On first commit, also inject a user text item that says what to speak,
    then trigger response.create. This guarantees TTS even if input audio is silent.
    """
    inbound_frames = 0
    appended_ms = 0.0
    committed = False
    last_event = None

    IN_RATE, OUT_RATE, WIDTH, CHANNELS = 8000, 16000, 2, 1

    def ulaw8k_to_pcm16_16k(b64_ulaw: str) -> bytes:
        try:
            ulaw = base64.b64decode(b64_ulaw)
        except Exception:
            return b""
        try:
            pcm8k = audioop.ulaw2lin(ulaw, WIDTH)            # μ-law -> PCM16 @ 8k
            pcm16k, _ = audioop.ratecv(pcm8k, WIDTH, CHANNELS, IN_RATE, OUT_RATE, None)  # -> 16k
            return pcm16k
        except Exception:
            return b""

    while True:
        try:
            msg = await asyncio.to_thread(twilio_ws.receive)
        except Exception as e:
            log("twilio.recv.exception", err=str(e)); break
        if msg is None:
            log("twilio.recv.none"); break

        try:
            data = json.loads(msg)
        except Exception:
            log("twilio.recv.parse_error", msg_preview=str(msg)[:80]); continue

        evt = data.get("event")
        if evt != last_event:
            log("twilio.event", type=evt)
            last_event = evt

        if evt == "start":
            sid = data.get("start", {}).get("streamSid")
            stream_info["sid"] = sid
            log("twilio.start", streamSid=sid, track=data.get("start", {}).get("tracks"))

        elif evt == "media":
            inbound_frames += 1
            b64_ulaw = data.get("media", {}).get("payload", "")
            pcm16k = ulaw8k_to_pcm16_16k(b64_ulaw)

            # append only if we have decoded audio
            if pcm16k:
                b64_pcm = base64.b64encode(pcm16k).decode("utf-8")
                try:
                    await openai_ws.send(json.dumps({
                        "type": "input_audio_buffer.append",
                        "audio": b64_pcm  # base64 of raw PCM16(16k) bytes
                    }))
                    appended_ms += 20.0  # ~20ms per Twilio frame
                    if inbound_frames <= 3 or inbound_frames % 20 == 0:
                        log("openai.append.ok", frames=inbound_frames,
                            approx_ms=int(appended_ms), energy=rms_pcm16(pcm16k), pcm_len=len(pcm16k))
                except Exception as e:
                    log("openai.append.error", err=str(e))
            else:
                if inbound_frames <= 3 or inbound_frames % 50 == 0:
                    log("transcode.empty_or_error", frame=inbound_frames)

            # Commit once and give the model explicit words to speak
            if not committed and appended_ms >= 800.0:
                try:
                    await openai_ws.send(json.dumps({"type": "input_audio_buffer.commit"}))
                    log("openai.input.commit", approx_ms=int(appended_ms))

                    # 👉 Inject explicit user message so the model has content to synthesize
                    await openai_ws.send(json.dumps({
                        "type": "conversation.item.create",
                        "item": {
                            "type": "message",
                            "role": "user",
                            "content": [
                                {"type": "input_text",
                                 "text": "Say exactly this one sentence: Hello from Escallop."}
                            ]
                        }
                    }))
                    log("force.tts.item.sent")

                    await openai_ws.send(json.dumps({"type": "response.create"}))
                    log("response.create.sent_after_commit_with_item")
                    committed = True
                except Exception as e:
                    log("openai.commit_or_response.error", err=str(e))

        elif evt == "stop":
            log("twilio.stop.recv"); break

    return



async def openai_to_twilio(twilio_ws, openai_ws, stream_info):
    """
    Reads OpenAI events; forwards audio deltas to Twilio.
    Logs per-event counts and previews to confirm audio generation.
    """
    counts = {}
    tw_media_sent = 0

    def bump(t): counts[t] = counts.get(t, 0) + 1

    def send_to_twilio(b64audio: str):
        nonlocal tw_media_sent
        sid = stream_info.get("sid")
        if not sid:
            log("twilio.media.skip", reason="no_streamSid_yet"); return
        try:
            payload = base64.b64encode(base64.b64decode(b64audio)).decode("utf-8")
        except Exception:
            payload = b64audio
        twilio_ws.send(json.dumps({"event":"media","streamSid":sid,"media":{"payload":payload}}))
        tw_media_sent += 1
        if tw_media_sent <= 3 or tw_media_sent % 20 == 0:
            log("twilio.media.sent", count=tw_media_sent, payload_preview=b64preview(payload))

    try:
        async for raw in openai_ws:
            try:
                evt = json.loads(raw)
            except Exception:
                log("openai.event.parse_error", raw_len=len(raw)); continue

            t = evt.get("type") or "?"
            bump(t)
            if sum(counts.values()) <= 10 or sum(counts.values()) % 50 == 0:
                log("openai.event", type=t)

            # check various audio delta shapes
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
                send_to_twilio(b64audio)

            if t in ("response.completed", "response.done"):
                log("openai.response.done_summary",
                    audio_frames=counts.get("response.output_audio.delta", 0),
                    total_events=sum(counts.values()))
    except Exception as e:
        log("openai.reader.exception", err=str(e))


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
        # emits once per second so we know the loop is alive
        while True:
            log("hb")
            await asyncio.sleep(DEBUG_HEARTBEAT_MS / 1000.0)

    def runner():
        asyncio.set_event_loop(loop)
        openai_ws = loop.run_until_complete(openai_realtime_connect())
        log("openai.connect.ok", model=OPENAI_REALTIME_MODEL or "gpt-realtime")

        # keep your current (PCM16 in @16k, PCMU out) session_update here
        # (don’t change what you already have working)
        session_update = {  # ✂️ use the same block you have now
            "type": "session.update",
            "session": {
                "type": "realtime",
                "model": OPENAI_REALTIME_MODEL or "gpt-realtime",
                "output_modalities": ["audio"],
                "audio": {
                    "input":  {"format": {"type": "audio/pcm", "sample_rate_hz": 16000}},
                    "output": {"format": {"type": "audio/pcmu"}, "voice": (OPENAI_VOICE or "alloy")}
                },
                "instructions": "You are a voice agent. Speak concise replies aloud."
            }
        }
        loop.run_until_complete(openai_ws.send(json.dumps(session_update)))
        log("session.update.sent",
            voice=OPENAI_VOICE or "alloy",
            audio_in="pcm16@16kHz",
            audio_out="audio/pcmu")

        # start tasks
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
