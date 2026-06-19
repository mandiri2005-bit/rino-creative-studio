FROM node:20-slim
WORKDIR /app

# ffmpeg/ffprobe for the Step 6 video stitch worker; chromium for the whiteboard
# Remotion render (Opt B — video/whiteboard). Both used by the video-worker service;
# harmless in the API image, which never invokes them (guarded).
RUN apt-get update && apt-get install -y --no-install-recommends ffmpeg chromium \
    fonts-dejavu-core fonts-liberation fonts-noto-core \
    && rm -rf /var/lib/apt/lists/*

# Remotion (whiteboard render) uses the system Chromium installed above instead of
# downloading its own — keeps the image lean and works offline in the container.
ENV REMOTION_BROWSER_EXECUTABLE=/usr/bin/chromium

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
