ARG RECSYS_BASE_IMAGE=recsys-base-python:local
FROM ${RECSYS_BASE_IMAGE}

WORKDIR /opt/recsys
COPY pyproject.toml uv.lock* /opt/recsys/
ENV PYTHONPATH=/opt/recsys/apps/data-platform/src:/opt/recsys/apps/data-platform/feature-store/src:/opt/recsys

RUN uv pip install --no-cache feast redis boto3 pyyaml pandas pyarrow

COPY . /opt/recsys

CMD ["python", "/opt/recsys/apps/data-platform/feature-store/src/validate_feature_store.py"]
