FROM node:20-slim
WORKDIR /app

# ffmpeg/ffprobe for the Step 6 video stitch worker (used by the video-worker
# service; harmless in the API image, which never invokes it — guarded).
RUN apt-get update && apt-get install -y --no-install-recommends ffmpeg \
    fonts-dejavu-core fonts-liberation fonts-noto-core \
    && rm -rf /var/lib/apt/lists/*

# Install dependencies
COPY package*.json ./
RUN npm install --omit=dev

# Copy all JS/MJS source files + the video engine package
COPY *.js *.mjs ./
COPY video ./video

RUN mkdir -p data narasi_temp output tts-output img-output

EXPOSE 3000
# Step 3: --import loads Sentry (instrument.mjs) before the app for ESM instrumentation.
# start.mjs branches on VIDEO_ROLE: 'worker' → BullMQ video workers, else the API.
CMD ["node", "--import", "./instrument.mjs", "start.mjs"]
