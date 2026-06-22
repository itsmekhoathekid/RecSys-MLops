ARG RECSYS_BASE_IMAGE=recsys-base-python:local
FROM ${RECSYS_BASE_IMAGE}

WORKDIR /opt/recsys
COPY pyproject.toml uv.lock* /opt/recsys/
ENV PYTHONPATH=/opt/recsys/apps/data-platform/data-generator/src:/opt/recsys

RUN uv pip install --no-cache pandas pyarrow pydantic pyyaml boto3 tqdm

COPY . /opt/recsys

CMD ["python", "/opt/recsys/apps/data-platform/data-generator/src/cli.py", "--help"]
