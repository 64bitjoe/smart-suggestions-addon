ARG BUILD_FROM
FROM $BUILD_FROM

# Install Python dependencies via apk (avoids PEP 668 externally-managed-environment error)
RUN apk add --no-cache python3 py3-aiohttp py3-websockets

# Copy source
WORKDIR /app
COPY src/ ./

# Register as s6 service — base image starts s6-overlay as PID 1
COPY run.sh /etc/services.d/smart_suggestions/run
RUN chmod a+x /etc/services.d/smart_suggestions/run
