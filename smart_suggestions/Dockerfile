ARG BUILD_FROM
FROM $BUILD_FROM

# Install Python dependencies via apk (avoids PEP 668 externally-managed-environment error)
RUN apk add --no-cache python3 py3-aiohttp py3-websockets

# Copy source
WORKDIR /app
COPY src/ ./
COPY run.sh /app/run.sh
RUN chmod a+x /app/run.sh

CMD ["/app/run.sh"]
