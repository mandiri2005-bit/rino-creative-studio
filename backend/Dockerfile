FROM node:20-slim
WORKDIR /app

# Install dependencies
COPY package*.json ./
RUN npm install --omit=dev

# Copy all JS/MJS source files
COPY *.js *.mjs ./

RUN mkdir -p data narasi_temp output tts-output img-output

EXPOSE 3000
CMD ["node", "server.js"]
