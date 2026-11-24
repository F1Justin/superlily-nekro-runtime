FROM local/nekro-agent:2.3.3-cloudflare-cache.7
ARG VCS_REF
LABEL org.opencontainers.image.revision=${VCS_REF}
COPY nekro_agent/api/message.py /app/nekro_agent/api/
COPY nekro_agent/models/db_chat_message.py /app/nekro_agent/models/
COPY nekro_agent/schemas/agent_ctx.py nekro_agent/schemas/agent_message.py /app/nekro_agent/schemas/
COPY nekro_agent/services/message_service.py /app/nekro_agent/services/
COPY nekro_agent/services/agent/run_agent.py /app/nekro_agent/services/agent/
COPY nekro_agent/services/agent/templates/compiler.py nekro_agent/services/agent/templates/history.py nekro_agent/services/agent/templates/image_history.py /app/nekro_agent/services/agent/templates/
