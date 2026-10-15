# BASE_IMAGE is deliberately an ARG: pick a vllm/vllm-openai tag you have verified exists
# (I could not verify tags from the build sandbox). Example: --build-arg BASE_IMAGE=vllm/vllm-openai:<tag>
ARG BASE_IMAGE=vllm/vllm-openai:latest
FROM ${BASE_IMAGE}
USER root
RUN apt-get update && apt-get install -y --no-install-recommends supervisor git bubblewrap util-linux \
    && rm -rf /var/lib/apt/lists/*
WORKDIR /app
COPY requirements.txt .
RUN python3 -m venv /opt/factory-venv && /opt/factory-venv/bin/pip install --no-cache-dir -r requirements.txt
ENV PATH="/opt/factory-venv/bin:$PATH"
COPY . .
RUN chmod +x scripts/start_vllm.sh && mkdir -p /workspace/factory
ENTRYPOINT []
CMD ["supervisord", "-c", "/app/deploy/supervisord.conf"]
