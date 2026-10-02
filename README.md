# AI Sales Gemini POC

Standalone Proof-of-Concept for connecting **Real Exotel Phone Calls** to **Google Gemini Live API** via bidirectional WebSockets.

---

## 1. What This POC Does

This project demonstrates real-time, low-latency, two-way conversational voice interaction:
1. An outbound call is triggered to a customer's phone using Exotel's Connect Voice AI API.
2. Exotel establishes a bidirectional WebSocket stream with this server using the AgentStream protocol.
3. Audio spoken by the customer (8 kHz PCM) is up-sampled to 16 kHz and streamed into Google Gemini Live.
4. Gemini Live produces audio responses (24 kHz PCM) in real-time, which are down-sampled to 8 kHz and streamed back to Exotel.
5. The customer hears Gemini speak naturally over the phone (configured for Malayalam with natural English mixing).

---

## 2. Architecture & Audio Flow

```
[ Customer Phone ]
       │
       ▼ (8 kHz PCM Audio)
[ Exotel Voice Cloud ]
       │
       ▼ (WSS AgentStream: base64-encoded linear16, 8000 Hz, mono)
[ FastAPI WebSocket: /exotel/media ]
       │
       ├──> Converter: 8 kHz PCM ──(Polyphase Resample 2:1)──> 16 kHz PCM
       │                                                             │
       │                                                             ▼
       │                                                  [ Google Gemini Live ]
       │                                                             │
       │                                                             ▼ (24 kHz PCM)
       ├──< Converter: 8 kHz PCM <──(Polyphase Resample 1:3)────────┘
       │
       ▼ (WSS AgentStream: base64-encoded linear16, 8000 Hz, mono)
[ Exotel Voice Cloud ]
       │
       ▼
[ Customer Hears Gemini ]
```

### Audio Specifications
- **Exotel Protocol**: 16-bit signed PCM, little-endian, mono, **8000 Hz**, base64 JSON payload.
- **Gemini Live Input**: 16-bit signed PCM, little-endian, mono, **16000 Hz**, raw binary chunks (`audio/pcm;rate=16000`).
- **Gemini Live Output**: 16-bit signed PCM, little-endian, mono, **24000 Hz**, raw binary chunks (`audio/pcm;rate=24000`).

---

## 3. Project Structure

```
ai-sales-gemini-poc/
├── app/
│   ├── __init__.py
│   ├── main.py               # FastAPI app, /health, /exotel/media, /test-call
│   ├── config.py             # Pydantic environment configuration
│   ├── gemini/
│   │   ├── __init__.py
│   │   └── live_session.py   # Gemini Live client and session lifecycle
│   ├── exotel/
│   │   ├── __init__.py
│   │   └── websocket.py      # AgentStream protocol parsing and bidirectional pump
│   └── audio/
│       ├── __init__.py
│       └── converter.py      # 8k <-> 16k <-> 24k resampling and base64 codecs
├── tests/
│   ├── __init__.py
│   ├── test_health.py
│   ├── test_audio_converter.py
│   ├── test_exotel_websocket.py
│   ├── test_gemini.py
│   ├── test_outbound.py
│   └── test_websocket_integration.py
├── .env.example
├── .gitignore
├── pyproject.toml
└── README.md
```

---

## 4. Installation with `uv`

Ensure [`uv`](https://github.com/astral-sh/uv) is installed on your system.

```bash
# Clone or navigate to the project directory
cd ai-sales-gemini-poc

# Install all dependencies into virtual environment
uv sync
```

---

## 5. Environment Variables Configuration

Copy `.env.example` to `.env`:

```bash
cp .env.example .env
```

Configure the following variables in `.env`:

| Variable | Description | Example / Default |
|---|---|---|
| `GEMINI_API_KEY` | Google AI Studio API key | `AIzaSy...` |
| `GEMINI_LIVE_MODEL` | Gemini Live model identifier | `gemini-3.8-live` (or `gemini-2.0-flash-exp`) |
| `EXOTEL_API_KEY` | Exotel API Key | From Exotel Dashboard |
| `EXOTEL_API_TOKEN` | Exotel API Token | From Exotel Dashboard |
| `EXOTEL_ACCOUNT_SID` | Exotel Account SID | `webcurve1` |
| `EXOTEL_EXOPHONE` | Your virtual ExoPhone caller ID | `049-542-69828` |
| `EXOTEL_REGION` | Exotel datacenter region | `india` or `singapore` |
| `PUBLIC_BASE_URL` | Public HTTPS/WSS URL of this server | `https://xxxx.ngrok-free.app` |
| `INITIAL_GREETING` | Opening greeting spoken by Gemini | `ഹലോ, നമസ്കാരം. ഇപ്പോൾ സംസാരിക്കാൻ ഫ്രീ ആണോ?` |
| `SYSTEM_INSTRUCTION` | System prompt controlling persona | Simple Malayalam persona instruction |

> [!CAUTION]
> Never commit `.env` containing live credentials to Git. `.gitignore` is pre-configured to ignore `.env`.

---

## 6. How to Run FastAPI & Expose Publicly

### Step 1: Start the Local FastAPI Server
Run with `uv`:
```bash
uv run uvicorn app.main:app --host 0.0.0.0 --port 8000 --reload
```

Verify the health check locally:
```bash
curl http://localhost:8000/health
# Response: {"status":"ok"}
```

### Step 2: Expose Port 8000 with a Public Tunnel (ngrok)
In another terminal, start ngrok:
```bash
ngrok http 8000
```
Note the forwarding URL provided by ngrok (e.g., `https://abc12345.ngrok-free.app`).

### Step 3: Update `PUBLIC_BASE_URL`
Set your public URL in `.env`:
```env
PUBLIC_BASE_URL=https://abc12345.ngrok-free.app
```
The server automatically derives the WebSocket stream URL as:
```
wss://abc12345.ngrok-free.app/exotel/media
```

---

## 7. How to Configure Exotel

### Outbound Calls (Using `/test-call`)
No manual Exotel dashboard applet setup is needed for testing outbound calls. The `/test-call` endpoint programmatically instructs Exotel's Connect Voice AI API to call the customer number and bind the call directly to `StreamUrl: wss://your-domain/exotel/media` with `StreamType: bidirectional`.

### Inbound Calls (Optional ExoPhone Applet)
If you wish to receive incoming calls to your ExoPhone:
1. In Exotel Dashboard, navigate to **App Bazaar** -> Create an App.
2. Add the **Voice AI / AgentStream** applet.
3. Configure the Stream URL to: `wss://<your-ngrok-domain>/exotel/media` with stream mode `bidirectional`.
4. Assign this applet to your ExoPhone virtual number.

---

## 8. How to Trigger a Real Test Call

Send a `POST` request to `/test-call`:

```bash
curl -X POST http://localhost:8000/test-call \
  -H "Content-Type: application/json" \
  -d '{"to_number": "+91XXXXXXXXXX"}'
```

Response:
```json
{
  "status": "initiated",
  "message": "Outbound call initiated successfully",
  "stream_url": "wss://xxxx.ngrok-free.app/exotel/media",
  "call_sid": "ca6b8...",
  "details": { ... }
}
```

When you answer the phone:
1. Exotel connects to `/exotel/media` WebSocket.
2. Server opens the Gemini Live session.
3. Gemini greets in Malayalam: *"ഹലോ, നമസ്കാരം. ഇപ്പോൾ സംസാരിക്കാൻ ഫ്രീ ആണോ?"*
4. Speak into your phone, and Gemini responds.

---

## 9. Running Tests

Run the comprehensive pytest suite (all external services mocked):
```bash
uv run pytest -v
```

---

## 10. Known Limitations

- **POC Scope**: This project is strictly a proof-of-concept proving the real-time audio pipeline (Exotel <-> Gemini Live).
- **Interruption / Barge-in**: Turn interruptions are detected and logged at `DEBUG` level. Dynamic audio frame cancellation on the Exotel outbound queue is planned for the next phase.
- **Single Turn Buffering**: Uses standard streaming chunking. No persistence, database, CRM, or lead scoring is included by design.

---

## 11. Official Documentation Links

- [Exotel AgentStream WebSocket Protocol](https://developer.exotel.com/docs/agentstream/websocket-protocol)
- [Exotel Connect Voice AI API](https://developer.exotel.com/docs/agentstream/connect-voice-ai)
- [Google Gemini Live API SDK](https://ai.google.dev/gemini-api/docs/live-api/get-started-sdk)
- [Google Gemini Live API Capabilities](https://ai.google.dev/gemini-api/docs/live-api/capabilities)
