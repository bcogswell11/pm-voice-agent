from flask import Flask, request, Response

app = Flask(__name__)

@app.get("/health")
def health():
    return {"status": "ok"}

@app.post("/voice")
def voice():
    twiml = """<?xml version="1.0" encoding="UTF-8"?>
<Response>
  <Say voice="Polly.Matthew">Hi! Your Escallop voice agent is online. This is a test.</Say>
  <Pause length="1"/>
  <Say voice="Polly.Matthew">We’ll hook up the full AI in the next step.</Say>
</Response>"""
    return Response(twiml, mimetype="text/xml")

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=8000)
