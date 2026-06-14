FROM node:20-slim
WORKDIR /app

# Install dependencies
COPY package*.json ./
RUN npm install --omit=dev

# Copy all JS/MJS source files
COPY *.js *.mjs ./

RUN mkdir -p data narasi_temp output tts-output img-output

EXPOSE 3000
# Step 3: --import loads Sentry (instrument.mjs) before the app for ESM instrumentation.
CMD ["node", "--import", "./instrument.mjs", "server.js"]
