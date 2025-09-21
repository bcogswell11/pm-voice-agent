from flask import Flask, Response
from flask_sock import Sock

app = Flask(__name__)
sock = Sock(app)

@app.get("/health")
def health():
    return {"status": "ok"}

@app.post("/voice")
def voice():
    twiml = """<?xml version="1.0" encoding="UTF-8"?>
<Response>
  <Say voice="Polly.Matthew">Streaming is not turned on yet. This is a test message to confirm the webhook works.</Say>
</Response>"""
    return Response(twiml, mimetype="text/xml")

@sock.route("/stream")
def stream(ws):
    # Echo back silence frames just to validate the WS upgrades without OpenAI
    # Twilio expects JSON frames; we’ll accept and discard for now.
    try:
        while True:
            msg = ws.receive()
            if msg is None:
                break
            # No-op
    except Exception:
        pass

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=8000)
