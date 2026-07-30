FROM node:24-bookworm-slim AS build
WORKDIR /app
COPY apps/demo-web/frontend/package.json apps/demo-web/frontend/package-lock.json ./
RUN npm ci
COPY apps/demo-web/backend/openapi.json /backend/openapi.json
COPY apps/demo-web/frontend/ ./
RUN npm run build

FROM nginxinc/nginx-unprivileged:1.30-alpine3.23
USER root
RUN apk upgrade --no-cache curl libcurl
USER 101
COPY apps/demo-web/frontend/nginx.conf /etc/nginx/conf.d/default.conf
COPY --from=build /app/dist /usr/share/nginx/html
EXPOSE 8080
