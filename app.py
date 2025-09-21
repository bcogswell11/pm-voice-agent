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
    Forward Twilio PCMU frames into OpenAI:
      - 'audio' MUST be a base64 STRING of raw bytes (no object)
      - Normalize Twilio payloads via decode->encode
      - Commit after enough frames, then trigger response
    """
    inbound_frames = 0
    appended_frames = 0
    committed = False
    last_event = None

    def normalize_b64(b64s: str) -> str:
        # Normalize to standard base64 so GA accepts it as bytes
        try:
            return base64.b64encode(base64.b64decode(b64s)).decode("utf-8")
        except Exception:
            # If decode fails, pass through original (still try)
            return b64s

    while True:
        try:
            msg = await asyncio.to_thread(twilio_ws.receive)
        except Exception as e:
            log("twilio.recv.exception", err=str(e))
            break

        if msg is None:
            log("twilio.recv.none")
            break

        try:
            data = json.loads(msg)
        except Exception:
            log("twilio.recv.parse_error", msg_preview=str(msg)[:80])
            continue

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
            payload = data.get("media", {}).get("payload", "")
            if inbound_frames <= 3 or inbound_frames % 200 == 0:
                log("twilio.media.in", count=inbound_frames, payload_preview=b64preview(payload))

            norm = normalize_b64(payload)

            # ---- GA expects: {"type":"input_audio_buffer.append", "audio":"<base64-bytes>"} ----
            try:
                await openai_ws.send(json.dumps({
                    "type": "input_audio_buffer.append",
                    "audio": norm
                }))
                appended_frames += 1
                if appended_frames <= 3 or appended_frames % 20 == 0:
                    log("openai.append.ok", appended=appended_frames, sample_preview=b64preview(norm))
            except Exception as e:
                log("openai.append.error", err=str(e))

            # Commit after sufficient frames to guarantee >100ms buffer.
            # Twilio frames are ~20ms, so 100 frames ≈ 2.0s (very safe).
            if not committed and appended_frames >= 100:
                try:
                    await openai_ws.send(json.dumps({"type": "input_audio_buffer.commit"}))
                    log("openai.input.commit", frames=appended_frames)

                    await openai_ws.send(json.dumps({"type": "response.create"}))
                    log("response.create.sent_after_commit")
                    committed = True
                except Exception as e:
                    log("openai.commit_or_response.error", err=str(e))

        elif evt == "stop":
            log("twilio.stop.recv")
            break

        else:
            if evt not in ("connected", "heartbeat", "mark"):
                log("twilio.event.other", raw=data)

    return



async def openai_to_twilio(twilio_ws, openai_ws, stream_info):
    """
    Read OpenAI events; forward audio deltas to Twilio.
    Emits detailed counters and previews.
    """
    oa_events = 0
    oa_audio_deltas = 0
    tw_media_sent = 0
    last_delta_len = 0

    def send_to_twilio(b64audio: str):
        nonlocal tw_media_sent, last_delta_len
        sid = stream_info.get("sid")
        if not sid:
            log("twilio.media.skip", reason="no_streamSid_yet", preview=b64preview(b64audio))
            return
        # Normalize base64 payload (guard against urlsafe or padding issues)
        try:
            payload = base64.b64encode(base64.b64decode(b64audio)).decode("utf-8")
        except Exception:
            payload = b64audio  # already normalized or not strictly padded
        last_delta_len = len(payload)
        try:
            twilio_ws.send(json.dumps({
                "event": "media",
                "streamSid": sid,
                "media": {"payload": payload}
            }))
            tw_media_sent += 1
            # Log first 3 frames, then every 20th to avoid noise
            if tw_media_sent <= 3 or tw_media_sent % 20 == 0:
                log("twilio.media.sent",
                    count=tw_media_sent,
                    streamSid=sid,
                    payload_preview=b64preview(payload))
        except Exception as e:
            log("twilio.media.error", err=str(e))

    try:
        async for raw in openai_ws:
            oa_events += 1
            t = None
            try:
                evt = json.loads(raw)
                t = evt.get("type")
            except Exception:
                log("openai.event.parse_error", raw_len=len(raw))
                continue

            # Log the first few events + every 50th event
            if oa_events <= 5 or oa_events % 50 == 0:
                log("openai.event", idx=oa_events, type=t)

            # Capture errors verbosely
            if t == "error":
                log("openai.error", raw=evt)
                return

            # Accept multiple delta shapes
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
                oa_audio_deltas += 1
                # Log first 3 deltas + every 20th
                if oa_audio_deltas <= 3 or oa_audio_deltas % 20 == 0:
                    log("openai.audio.delta",
                        idx=oa_audio_deltas,
                        preview=b64preview(b64audio))
                send_to_twilio(b64audio)

            # When a response completes with zero audio, log it explicitly
            if t in ("response.completed", "response.done"):
                if oa_audio_deltas == 0:
                    log("openai.response.done_no_audio")
                else:
                    log("openai.response.done_with_audio", deltas=oa_audio_deltas)

    except Exception as e:
        log("openai.reader.exception", err=str(e))

    # Graceful stop
    sid = stream_info.get("sid")
    if sid:
        try:
            twilio_ws.send(json.dumps({"event": "stop", "streamSid": sid}))
            log("twilio.stop.sent", streamSid=sid)
        except Exception as e:
            log("twilio.stop.error", err=str(e))

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
        log("openai.connect.ok", model=OPENAI_REALTIME_MODEL or "gpt-realtime")

        # GA session.update matching Twilio’s reference (PCMU + server VAD + voice)
        session_update = {
            "type": "session.update",
            "session": {
                "type": "realtime",
                "model": OPENAI_REALTIME_MODEL or "gpt-realtime",
                "output_modalities": ["audio"],
                "audio": {
                    "input": {
                        "format": {"type": "audio/pcmu"},
                        "turn_detection": {"type": "server_vad"}
                    },
                    "output": {
                        "format": {"type": "audio/pcmu"},
                        "voice": (OPENAI_VOICE or "alloy")
                    }
                },
                "instructions": "You are a voice agent. Speak concise replies aloud."
            }
        }
        loop.run_until_complete(openai_ws.send(json.dumps(session_update)))
        log("session.update.sent", voice=OPENAI_VOICE or "alloy",
            audio_in="audio/pcmu", audio_out="audio/pcmu")

        # Start OpenAI->Twilio reader FIRST so we don't miss early deltas
        stream_info = {"sid": None}
        recv_task = loop.create_task(openai_to_twilio(ws, openai_ws, stream_info))
        loop.run_until_complete(asyncio.sleep(0))

        # START sending Twilio media → OpenAI; this function now appends, commits, and triggers response.create once.
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
