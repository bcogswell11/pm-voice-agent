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

async def openai_to_twilio_pcmout(twilio_ws, openai_ws, stream_info):
    """
    OpenAI -> Twilio (PCM16@24k -> PCMU@8k) with buffering until Twilio streamSid exists,
    and verbose event logging so we can SEE whether audio deltas arrive.
    """
    import audioop, base64, json, time

    WIDTH, CH = 2, 1
    IN_RATE, OUT_RATE = 24000, 8000

    pending_ulaw = []   # buffer deltas arriving before Twilio gives us streamSid
    sent = 0

    def to_ulaw8k(b64_pcm16_24k: str) -> str:
        try:
            pcm16 = base64.b64decode(b64_pcm16_24k)
            if not pcm16:
                return ""
            pcm8k, _ = audioop.ratecv(pcm16, WIDTH, CH, IN_RATE, OUT_RATE, None)
            ulaw = audioop.lin2ulaw(pcm8k, WIDTH)
            return base64.b64encode(ulaw).decode("utf-8")
        except Exception as e:
            print(f"[pcmout] transcode.error {e}")
            return ""

    def flush_if_ready():
        nonlocal sent
        sid = stream_info.get("sid")
        if not sid or not pending_ulaw:
            return
        # Trickle with ~20ms pacing so Twilio plays smoothly
        while pending_ulaw:
            payload = pending_ulaw.pop(0)
            try:
                twilio_ws.send(json.dumps({"event":"media","streamSid":sid,"media":{"payload":payload}}))
                sent += 1
                if sent in (1,2,3,10,25,50) or sent % 50 == 0:
                    print(f"[pcmout] twilio.media.sent count={sent} preview={payload[:24]}...")
                time.sleep(0.020)
            except Exception as e:
                print(f"[pcmout] twilio.media.error {e}")
                break

    try:
        async for raw in openai_ws:
            try:
                evt = json.loads(raw)
            except Exception:
                continue

            t = evt.get("type") or "?"
            # Verbose diag: show every event type to confirm what the server is sending
            print(f"[pcmout.diag] openai.event type={t}")

            if t == "error":
                print("[pcmout] openai.error", evt)
                continue

            # Known audio delta shapes (different docs/sdks use different names)
            b64_pcm = None
            if t in ("response.output_audio.delta", "response.audio.delta"):
                b64_pcm = evt.get("delta")
                if isinstance(b64_pcm, dict):  # some SDKs nest "audio"
                    b64_pcm = b64_pcm.get("audio")
            elif t in ("response.output_item.delta", "response.delta"):
                maybe = evt.get("delta")
                if isinstance(maybe, dict):
                    b64_pcm = maybe.get("audio")
            elif isinstance(evt.get("data"), dict):
                b64_pcm = evt["data"].get("audio")

            if b64_pcm:
                b64_ulaw = to_ulaw8k(b64_pcm)
                if b64_ulaw:
                    if not stream_info.get("sid"):
                        pending_ulaw.append(b64_ulaw)  # buffer until we have a sid
                    else:
                        pending_ulaw.append(b64_ulaw)
                        flush_if_ready()

            if t in ("response.done", "response.completed"):
                flush_if_ready()
                print(f"[pcmout] openai response done (sent_frames={sent})")
    except Exception as e:
        print(f"[pcmout] openai.reader.exception {e}")

    flush_if_ready()
    sid = stream_info.get("sid")
    if sid:
        try:
            twilio_ws.send(json.dumps({"event":"stop","streamSid":sid}))
        except Exception:
            pass

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
    Read OpenAI events. Forward audio *if* we get audio deltas.
    Also log text deltas so we know if the model is replying in text only.
    """
    text_chars = 0
    audio_frames = 0

    def send_ulaw_b64_to_twilio(b64audio: str):
        sid = stream_info.get("sid")
        if not sid:
            print("[twilio<-openai] SKIP (no streamSid yet)")
            return
        # Normalize/validate base64
        try:
            raw = base64.b64decode(b64audio)
            # re-encode to keep Twilio happy if upstream b64 had newlines/etc.
            payload = base64.b64encode(raw).decode("utf-8")
        except Exception:
            payload = b64audio  # best effort
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

            # --- DIAGNOSTIC LOGGING ---
            if t in ("response.created", "response.done", "session.updated", "session.created"):
                print("[openai.diag] event", json.dumps({"type": t}))

            # text stream?
            if t in ("response.output_text.delta", "response.text.delta"):
                delta = evt.get("delta") or ""
                text_chars += len(delta)
                if text_chars <= 200:
                    print(f"[openai.text] +{len(delta)} chars (total={text_chars})")
                continue

            # audio stream? (GA names vary)
            b64audio = None
            if t in ("response.audio.delta", "response.output_audio.delta"):
                b64audio = evt.get("delta")
            elif t in ("response.output_item.delta", "response.delta"):
                maybe = evt.get("delta")
                if isinstance(maybe, dict):
                    b64audio = maybe.get("audio")

            if b64audio:
                audio_frames += 1
                # Peek first few bytes for sanity
                try:
                    peek = base64.b64decode(b64audio)[:16]
                    print(f"[openai.audio] frame#{audio_frames} peek={list(peek)}")
                except Exception:
                    pass
                # Forward as-is (expects μ-law base64). If you set session output to PCMU,
                # the payload is already μ-law@8k, which Twilio requires. :contentReference[oaicite:2]{index=2}
                send_ulaw_b64_to_twilio(b64audio)
                continue

            if t == "error":
                print("[openai.diag] error", json.dumps({"raw": evt}))
                # Don't return immediately; keep loop so we can catch any follow-ups
                continue

        # loop exhausted
        sid = stream_info.get("sid")
        if sid:
            try:
                twilio_ws.send(json.dumps({"event": "stop", "streamSid": sid}))
            except Exception:
                pass
        print(f"[openai.summary] text_chars={text_chars} audio_frames={audio_frames}")
    except Exception as e:
        print("[openai_to_twilio] exception:", repr(e))




# --- WebSocket: Twilio connects here ---
@sock.route("/stream_pcmout")
def stream_pcmout(ws):
    """
    Separate test path:
      - INPUT to OpenAI: audio/pcmu (forward Twilio frames as-is)
      - OUTPUT from OpenAI: audio/pcm @24k (we transcode to μ-law 8k for Twilio)
      - audio-only, server VAD ON
      - WAIT for Twilio 'start' (streamSid) and then:
          1) conversation.item.create (user says: "Say exactly: Hello from Escallop.")
          2) response.create   (no inline instructions)
    """
    import asyncio, json, threading, time

    if not OPENAI_API_KEY:
        try: ws.send(json.dumps({"event":"stop"}))
        except Exception: pass
        return

    loop = asyncio.new_event_loop()

    def runner():
        asyncio.set_event_loop(loop)
        openai_ws = loop.run_until_complete(openai_realtime_connect())
        print("[pcmout] openai connected")

        session_update = {
            "type":"session.update",
            "session":{
                "type":"realtime",
                "model": OPENAI_REALTIME_MODEL or "gpt-realtime",
                "output_modalities":["audio"],  # single modality, per model rules
                "audio":{
                    "input":{
                        "format": {"type":"audio/pcmu"},
                        "turn_detection": {"type":"server_vad", "silence_duration_ms": 500}
                    },
                    "output":{
                        "format": {"type":"audio/pcm", "rate": 24000},  # model demanded >=24k
                        "voice": (OPENAI_VOICE or "alloy")
                    }
                },
                "instructions":"You are a voice agent. Speak out loud; keep replies brief."
            }
        }
        loop.run_until_complete(openai_ws.send(json.dumps(session_update)))
        print("[pcmout] session.update sent (in=pcmu, out=pcm@24k)")

        # Start OpenAI -> Twilio reader FIRST
        stream_info = {"sid": None}
        recv_task = loop.create_task(openai_to_twilio_pcmout(ws, openai_ws, stream_info))
        loop.run_until_complete(asyncio.sleep(0))

        # Twilio -> OpenAI: forward PCMU, capture sid ASAP
        async def twilio_to_openai_pcmu():
            frames = 0
            while True:
                try:
                    raw = await asyncio.to_thread(ws.receive)
                except Exception as e:
                    print(f"[pcmout] twilio.recv.exception {e}"); break
                if raw is None:
                    print("[pcmout] twilio ws closed"); break
                try:
                    msg = json.loads(raw)
                except Exception:
                    continue

                evt = msg.get("event")
                if evt == "start":
                    stream_info["sid"] = msg.get("start",{}).get("streamSid")
                    print(f"[pcmout] twilio.start sid={stream_info['sid']}")
                elif evt == "media":
                    frames += 1
                    if frames in (1,2,3) or frames % 50 == 0:
                        print(f"[pcmout] twilio.media.in {frames}")
                    payload = msg.get("media",{}).get("payload")
                    if payload:
                        try:
                            await openai_ws.send(json.dumps({
                                "type":"input_audio_buffer.append",
                                "audio": payload
                            }))
                        except Exception as e:
                            print(f"[pcmout] openai.append.error {e}")
                elif evt == "stop":
                    print("[pcmout] twilio.stop"); break

        send_task = loop.create_task(twilio_to_openai_pcmu())

        # ✅ WAIT for Twilio 'start', then message+response
        for _ in range(100):  # ~2s
            if stream_info.get("sid"):
                break
            loop.run_until_complete(asyncio.sleep(0.02))

        if stream_info.get("sid"):
            # 1) add a user message to the conversation
            loop.run_until_complete(openai_ws.send(json.dumps({
                "type": "conversation.item.create",
                "item": {
                    "type": "message",
                    "role": "user",
                    "content": [
                        { "type": "input_text", "text": "Say exactly: Hello from Escallop." }
                    ]
                }
            })))
            print("[pcmout] conversation.item.create sent")
            # 2) trigger response (no inline instructions)
            loop.run_until_complete(openai_ws.send(json.dumps({ "type": "response.create" })))
            print("[pcmout] response.create sent")
        else:
            print("[pcmout] WARN: no streamSid after wait; skipping forced TTS")

        loop.run_until_complete(asyncio.gather(send_task, recv_task, return_exceptions=True))
        try: loop.run_until_complete(openai_ws.close())
        except Exception: pass

    t = threading.Thread(target=runner, daemon=True)
    t.start()
    try:
        while t.is_alive():
            time.sleep(0.05)
    except Exception:
        pass

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

        # Session: PCMU (μ-law) in/out, audio-only, server VAD ON
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

        # Start OpenAI -> Twilio reader FIRST so we don't miss early deltas
        stream_info = {"sid": None}
        recv_task = loop.create_task(openai_to_twilio(ws, openai_ws, stream_info))
        loop.run_until_complete(asyncio.sleep(0))

        # Force a spoken reply (per-response options; GA schema requires nested 'response')
        loop.run_until_complete(openai_ws.send(json.dumps({
            "type": "response.create",
            "response": {
                "instructions": "Say exactly: Hello! This is a forced audio probe.",
                "modalities": ["audio"]
            }
        })))
        print("[stream] response.create sent (force audio)")

        # Twilio -> OpenAI (append PCMU frames; NO manual commit — server VAD will commit)
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
