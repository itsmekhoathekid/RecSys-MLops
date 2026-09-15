FROM python:3.12-slim
WORKDIR /app
COPY apps/agentic/llm_ab_router/requirements.txt /tmp/requirements.txt
RUN pip install --no-cache-dir -r /tmp/requirements.txt
COPY jenkins/python/__init__.py /app/jenkins/python/__init__.py
COPY jenkins/python/model_cd /app/jenkins/python/model_cd
COPY jenkins/python/llm_agent_cd /app/jenkins/python/llm_agent_cd
COPY apps/agentic/llm_ab_router /app/apps/agentic/llm_ab_router
RUN python -m apps.agentic.llm_ab_router.compile_protocol
COPY configs/llm-ab /app/configs/llm-ab
COPY jenkins/LLMAgentCD.Jenkinsfile /app/jenkins/LLMAgentCD.Jenkinsfile
COPY jenkins/LLMWorkflowCD.Jenkinsfile /app/jenkins/LLMWorkflowCD.Jenkinsfile
COPY jenkins/LLMCandidateOnboard.Jenkinsfile /app/jenkins/LLMCandidateOnboard.Jenkinsfile
COPY jenkins/RecommendationBaselineMigration.Jenkinsfile /app/jenkins/RecommendationBaselineMigration.Jenkinsfile
COPY jenkins/GlobalModelConfig.Jenkinsfile /app/jenkins/GlobalModelConfig.Jenkinsfile
COPY jenkins/scripts /app/jenkins/scripts
COPY infra/helm/recsys-global-model-config /app/infra/helm/recsys-global-model-config
COPY infra/helm/recsys-kagent-agent /app/infra/helm/recsys-kagent-agent
COPY infra/helm/recsys-recommendation-agent /app/infra/helm/recsys-recommendation-agent
COPY infra/helm/recsys-coordinator-agent /app/infra/helm/recsys-coordinator-agent
COPY infra/helm/recsys-workflow-ab /app/infra/helm/recsys-workflow-ab
COPY infra/helm/recsys-llm-ab /app/infra/helm/recsys-llm-ab
COPY infra/helm/recsys-observability/dashboards/llm-ab-rollout.json /app/infra/helm/recsys-observability/dashboards/llm-ab-rollout.json
ENV PYTHONUNBUFFERED=1 PORT=80
EXPOSE 80
CMD ["python", "-m", "apps.agentic.llm_ab_router.server"]
