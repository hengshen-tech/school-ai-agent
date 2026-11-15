FROM python:3.12-slim

# 国内构建可以传入镜像源：docker compose build --build-arg PIP_INDEX_URL=https://pypi.tuna.tsinghua.edu.cn/simple
ARG PIP_INDEX_URL=https://pypi.org/simple
ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1 DATA_DIR=/data

RUN useradd --create-home --uid 1000 app && mkdir -p /data && chown app:app /data
WORKDIR /srv

COPY backend/requirements.txt backend/requirements.txt
# claude-agent-sdk 的 wheel 自带 Claude Code 可执行文件，不需要另装 Node.js
RUN pip install --no-cache-dir -i ${PIP_INDEX_URL} -r backend/requirements.txt

COPY backend backend
COPY frontend frontend

USER app
WORKDIR /srv/backend
EXPOSE 8001
VOLUME ["/data"]
HEALTHCHECK --interval=30s --timeout=5s --start-period=10s \
  CMD python -c "import urllib.request;urllib.request.urlopen('http://127.0.0.1:8001/api/health')" || exit 1
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8001"]
