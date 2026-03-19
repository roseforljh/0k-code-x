FROM node:20-alpine AS build

WORKDIR /app

COPY webui/frontend/package*.json ./
RUN npm ci

COPY webui/frontend/ ./

ARG VITE_API_BASE=/api
ENV VITE_API_BASE=${VITE_API_BASE}

RUN npm run build

FROM scratch AS export

COPY --from=build /app/dist /dist
