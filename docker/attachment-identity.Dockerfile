FROM superlily/nekro-agent:2.3.3-superlily.14-argon-cache.1
COPY nekro_agent/adapters/onebot_v11/tools/attachments.py nekro_agent/adapters/onebot_v11/tools/convertor.py /app/nekro_agent/adapters/onebot_v11/tools/
COPY nekro_agent/adapters/onebot_v11/matchers/message.py /app/nekro_agent/adapters/onebot_v11/matchers/
ARG VCS_REF=unknown
LABEL org.opencontainers.image.version="2.3.3-superlily.15-attachment-identity.1"
LABEL org.opencontainers.image.revision="${VCS_REF}"
