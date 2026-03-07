ARG BUILD_FROM
FROM $BUILD_FROM

# Install Python dependencies via apk (avoids PEP 668 externally-managed-environment error)
RUN apk add --no-cache python3 py3-aiohttp py3-websockets

# Copy source
WORKDIR /app
COPY run.sh ./
COPY src/ ./
RUN chmod +x /app/run.sh

CMD ["/app/run.sh"]
