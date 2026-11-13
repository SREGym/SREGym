FROM golang:1.24-bookworm AS build
WORKDIR /src
COPY go.mod go.sum ./
RUN go mod download
COPY shared shared
COPY cmd cmd
COPY producer producer
RUN CGO_ENABLED=0 go build -trimpath -ldflags='-s -w' -o /out/ ./cmd/...
FROM debian:bookworm-slim AS runtime
COPY --from=build /out/ /usr/local/bin/
FROM python:3.12-slim AS helper
RUN pip install --no-cache-dir psycopg[binary]==3.2.13 confluent-kafka==2.15.1
