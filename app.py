from flask import Flask, Response

app = Flask(__name__)

@app.get("/health")
def health():
    return {"status": "ok"}

@app.post("/voice")
def voice():
    twiml = """<?xml version="1.0" encoding="UTF-8"?>
<Response>
  <Say>Test line. If you hear this, the webhook works.</Say>
</Response>"""
    return Response(twiml, mimetype="text/xml")

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=8000)
