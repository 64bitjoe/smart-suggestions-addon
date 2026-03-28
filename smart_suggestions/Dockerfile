ARG BUILD_FROM
FROM $BUILD_FROM

# Install Python and core packages via apk
RUN apk add --no-cache python3 py3-pip py3-aiohttp py3-yaml

# Install AI SDKs via pip (not in Alpine repos)
RUN pip3 install --no-cache-dir --break-system-packages anthropic openai aiosqlite

# Copy source
WORKDIR /app
COPY src/ ./

# Copy seed patterns for first-run bootstrap
COPY seed_patterns.json /opt/smart_suggestions/seed_patterns.json

# Register as s6 service
COPY run.sh /etc/services.d/smart_suggestions/run
RUN chmod a+x /etc/services.d/smart_suggestions/run
