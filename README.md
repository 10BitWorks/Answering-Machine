# Phone Assistant with Gemini Live 3.1 and Pipecat

This project implements a locally hosted Pipecat instance that routes audio between Twilio and the Gemini Live API using the Gemini 3.1 live audio model.

## Prerequisites

1. **Python 3.10+**
2. **Twilio Account**: A phone number with voice capabilities and a TwiML App.
3. **Google AI Studio API Key**: Access to the Gemini API.
4. **Nginx Proxy Manager (NPM)**: Or a similar reverse proxy to handle SSL and WebSockets.

## Installation & Deployment (Portainer)

1. **Create Stack**: In Portainer, create a new Stack using this repository.
2. **Environment Variables**: Add the following variables in the Portainer "Environment variables" section:
   - `GOOGLE_API_KEY`
   - `TWILIO_ACCOUNT_SID`
   - `TWILIO_AUTH_TOKEN`
   *Portainer will automatically write these to `stack.env` inside the container.*
3. **Deploy**: Deploy the stack. The bot will listen on host port `17293`.

## Reverse Proxy Configuration (NPM)

1. **Proxy Host**: Create a new Proxy Host in Nginx Proxy Manager.
2. **Details**:
   - **Forward Host/IP**: The local IP of your Docker host.
   - **Forward Port**: `17293`
   - **Websockets Support**: **ENABLED** (Required for audio streaming).
3. **SSL**: Enable "Force SSL" and ensure you have a valid certificate (required by Twilio).

## Twilio Configuration

1. **TwiML App**: In the Twilio Console, create or update a TwiML App.
2. **Voice Request URL**: Set this to `https://your-public-domain.com/twiml`.
3. **Method**: `HTTP GET`.
4. **Phone Number**: Assign your TwiML App to your active Twilio phone number.

## How it Works

- When a call comes in, Twilio sends a GET request to `/twiml`.
- The server responds with TwiML instructions to connect the call to a WebSocket stream (`/ws`).
- Pipecat's `FastAPIWebsocketTransport` handles the bidirectional audio stream from Twilio.
- The `GeminiLiveLLMService` interacts with `gemini-3.8-live` to provide real-time speech-to-speech AI interaction.
