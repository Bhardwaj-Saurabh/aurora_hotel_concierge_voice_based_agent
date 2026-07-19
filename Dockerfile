# Aurora talk server (goal.md 4.1) — the current serving surface: LiveKit
# tokens + the /agent and /voice-agent turn endpoints. The room-native agent
# worker gets its own image in Phase 3.1.
#
# Deliberately installs ONLY livekit/requirements.txt: the browser does VAD
# and capture, so the server needs no audio stack (sounddevice/webrtcvad/
# numpy) — which also means no C toolchain and a slim base image.
#
#   docker build -t aurora-talk-server .
#   docker run --rm -p 5173:5173 \
#     -e PROVIDER=mock \                     # or openai/groq + the API key
#     -e LIVEKIT_URL=ws://host.docker.internal:7880 \
#     aurora-talk-server

FROM python:3.12-slim

WORKDIR /app

COPY livekit/requirements.txt livekit/requirements.txt
RUN pip install --no-cache-dir -r livekit/requirements.txt

COPY pipeline/ pipeline/
COPY knowledge/ knowledge/
COPY livekit/ livekit/

ENV PROVIDER=mock \
    TALK_HOST=0.0.0.0 \
    TALK_PORT=5173 \
    TELEMETRY_JSONL=/app/logs/voice-events.jsonl \
    BOOKINGS_DB=/app/logs/bookings.db

EXPOSE 5173

# Never run as root; logs dir is the only writable path the app needs.
RUN mkdir -p /app/logs && useradd --create-home aurora && chown -R aurora /app/logs
USER aurora

CMD ["python", "livekit/talk_server.py"]
