FROM python:3.12-slim
RUN apt-get update && apt-get install -y --no-install-recommends git && rm -rf /var/lib/apt/lists/*
WORKDIR /app
COPY requirements.lock ./
RUN pip install --no-cache-dir -r requirements.lock
COPY pyproject.toml ./
COPY src ./src
RUN pip install --no-cache-dir --no-deps . && useradd -m -u 10001 agent && mkdir /data && chown agent /data
USER agent
ENV DATA_DIR=/data
EXPOSE 8080
CMD ["uvicorn", "repo_agent.api:app", "--host", "0.0.0.0", "--port", "8080"]
