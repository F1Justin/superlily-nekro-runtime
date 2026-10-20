FROM superlily/nekro-agent:2.3.3-superlily.15-attachment-identity.1
COPY nekro_agent/services/agent/deepseek_search.py /app/nekro_agent/services/agent/
COPY nekro_agent/services/plugin/base.py /app/nekro_agent/services/plugin/
COPY nekro_agent/services/plugin/schema.py /app/nekro_agent/services/plugin/
COPY nekro_agent/services/rpc_service.py /app/nekro_agent/services/
COPY nekro_agent/services/sandbox/rpc_grants.py nekro_agent/services/sandbox/runner.py /app/nekro_agent/services/sandbox/
COPY plugins/builtin/deepseek_search.py /app/plugins/builtin/
ARG VCS_REF=unknown
LABEL org.opencontainers.image.version="2.3.3-superlily.16-deepseek-search.2"
LABEL org.opencontainers.image.revision="${VCS_REF}"
