ARG BUILD_FROM
FROM $BUILD_FROM

# Install Python dependencies
RUN apk add --no-cache python3 py3-pip && \
    pip3 install --no-cache-dir aiohttp websockets

# Copy source
WORKDIR /app
COPY src/ ./

CMD ["/app/run.sh"]
