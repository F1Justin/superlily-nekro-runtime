FROM superlily/nekro-agent:2.3.3-superlily.13-egress.1
COPY nekro_agent/services/agent/cache_policy.py nekro_agent/services/agent/creator.py nekro_agent/services/agent/openai.py nekro_agent/services/agent/run_agent.py /app/nekro_agent/services/agent/
COPY nekro_agent/services/agent/templates/history.py nekro_agent/services/agent/templates/compiler.py nekro_agent/services/agent/templates/system.py /app/nekro_agent/services/agent/templates/
COPY nekro_agent/services/agent/templates/j2/policy_kernel.j2 /app/nekro_agent/services/agent/templates/j2/
COPY nekro_agent/services/agent/templates/j2_zh/policy_kernel.j2 /app/nekro_agent/services/agent/templates/j2_zh/
ARG VCS_REF=unknown
LABEL org.opencontainers.image.version="2.3.3-superlily.14-argon-cache.1"
LABEL org.opencontainers.image.revision="${VCS_REF}"
