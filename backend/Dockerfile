FROM node:20-slim
WORKDIR /app

# ffmpeg/ffprobe for the Step 6 video stitch worker (used by the video-worker
# service; harmless in the API image, which never invokes it — guarded).
RUN apt-get update && apt-get install -y --no-install-recommends ffmpeg \
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
CMD ["node", "--import", "./instrument.mjs", "server.js"]
