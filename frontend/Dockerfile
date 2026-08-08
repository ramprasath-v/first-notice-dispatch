FROM node:22-alpine AS build

WORKDIR /app
COPY package.json package-lock.json ./
RUN npm ci
COPY . .
ARG API_BASE_URL
ENV API_BASE_URL=${API_BASE_URL}
RUN test -n "$API_BASE_URL" \
    && node -e 'const fs = require("fs"); fs.writeFileSync("public/config.js", `window.__FIRSTNOTICE_CONFIG__ = ${JSON.stringify({ apiBaseUrl: process.env.API_BASE_URL })};\n`);'
RUN npm run build

FROM nginx:alpine

COPY nginx.conf /etc/nginx/conf.d/default.conf
COPY config.template.js /etc/firstnotice/config.template.js
COPY --from=build /app/dist/frontend/browser /usr/share/nginx/html

EXPOSE 8080

CMD ["/bin/sh", "-c", "test -n \"$API_BASE_URL\" && envsubst '$API_BASE_URL' < /etc/firstnotice/config.template.js > /usr/share/nginx/html/config.js && exec nginx -g 'daemon off;'"]
