FROM local/nekro-agent:2.3.3-security-manager.1 AS runtime
COPY nekro_agent/services/agent/cache_policy.py /app/nekro_agent/services/agent/cache_policy.py
COPY nekro_agent/services/agent/usage_accounting.py /app/nekro_agent/services/agent/usage_accounting.py
COPY nekro_agent/services/agent/openai.py /app/nekro_agent/services/agent/openai.py
COPY nekro_agent/services/agent/run_agent.py /app/nekro_agent/services/agent/run_agent.py
COPY nekro_agent/services/agent/templates/history.py /app/nekro_agent/services/agent/templates/history.py
COPY nekro_agent/schemas/sandbox.py /app/nekro_agent/schemas/sandbox.py
RUN chmod a+r /app/nekro_agent/services/agent/cache_policy.py \
    /app/nekro_agent/services/agent/usage_accounting.py \
    /app/nekro_agent/services/agent/openai.py \
    /app/nekro_agent/services/agent/run_agent.py \
    /app/nekro_agent/services/agent/templates/history.py \
    /app/nekro_agent/schemas/sandbox.py

# Build with --build-context webui=frontend/dist after poe frontend-build.
FROM runtime AS runtime-webui
COPY --from=webui / /app/static/
