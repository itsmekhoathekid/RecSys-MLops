FROM python:3.11-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PYTHONPATH=/opt/recsys/apps/api-serving/src

WORKDIR /opt/recsys

RUN pip install --no-cache-dir \
    fastapi \
    numpy \
    pydantic \
    redis \
    tritonclient[grpc] \
    uvicorn

COPY apps/api-serving /opt/recsys/apps/api-serving

CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8080"]
