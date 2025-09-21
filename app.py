# --- Imports ---
from flask import Flask, Response
from flask_sock import Sock
import os
import json

# --- App setup ---
app = Flask(__name__)
sock = Sock(app)

# --- Health check route ---
@app.get("/health")
def health():
    return {"status": "ok"}

# --- Normal /voice route (Say-only, your working one) ---
@app.post("/voice")
def voice():
    twiml = """<?xml version="1.0" encoding="UTF-8"?>
<Response>
  <Say voice="Polly.Matthew">Streaming is not turned on yet. This is a test message to confirm the webhook works.</Say>
</Response>"""
    return Response(twiml, mimetype="text/xml")

# --- /voice_stream_test route (only for stream testing) ---
@app.post("/voice_stream_test")
def voice_stream_test():
    base = os.getenv("PUBLIC_BASE_URL", "").rstrip("/")
    if base.startswith("https://"):
        stream_url = "wss://" + base[len("https://"):] + "/stream"
    elif base.startswith("http://"):
        stream_url = "ws://" + base[len("http://"):] + "/stream"
    else:
        # Fallback: your known Heroku URL (keep this updated if app name changes)
        stream_url = "wss://escallop-voice-pm-aa62425200e7.herokuapp.com/stream"

    twiml = f"""<?xml version="1.0" encoding="UTF-8"?>
<Response>
  <Connect>
    <Stream url="{stream_url}"/>
  </Connect>
</Response>"""
    return Response(twiml, mimetype="text/xml")

# --- WebSocket /stream route (accepts Twilio Media Stream frames; no audio returned yet) ---
@sock.route("/stream")
def stream(ws):
    starts = medias = stops = 0
    try:
        while True:
            msg = ws.receive()
            if msg is None:
                break
            try:
                data = json.loads(msg)
            except Exception:
                continue

            evt = data.get("event")
            if evt == "start":
                starts += 1
            elif evt == "media":
                medias += 1
            elif evt == "stop":
                stops += 1
                break
    except Exception:
        pass
    # Helpful debug line (shows up in Heroku logs)
    print(f"[stream] start={starts}, media={medias}, stop={stops}")

# --- Main (local only; Heroku uses Procfile) ---
if __name__ == "__main__":
    app.run(host="0.0.0.0", port=8000)
