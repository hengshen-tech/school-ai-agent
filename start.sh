#!/usr/bin/env bash
# A道工作台 启动脚本
#
#   ./start.sh            构建并启动（Docker），等服务就绪后打印访问地址
#   ./start.sh stop       停止
#   ./start.sh restart    重启（会重新构建）
#   ./start.sh logs       查看日志
#   ./start.sh status     查看运行状态
#   ./start.sh local      不用 Docker，直接用本机 Python 启动（开发用）
#
# 可选环境变量（也可以写在同目录的 .env 文件里）：
#   PORT=8001                 访问端口
#   APP_PASSWORD=xxx          访问口令（建议设置）
#   LLM_API_KEY=sk-...        预置模型 Key；不设的话启动后在页面「模型设置」里填
#   PIP_INDEX_URL=https://pypi.tuna.tsinghua.edu.cn/simple   国内构建加速
set -euo pipefail
cd "$(dirname "$0")"

if [ -f .env ]; then set -a; . ./.env; set +a; fi
PORT="${PORT:-8001}"
export PORT
URL="http://localhost:${PORT}"

say()  { printf '\033[1;32m==>\033[0m %s\n' "$*"; }
fail() { printf '\033[1;31m错误：\033[0m%s\n' "$*" >&2; exit 1; }

compose() {
  if docker compose version >/dev/null 2>&1; then docker compose "$@"
  elif command -v docker-compose >/dev/null 2>&1; then docker-compose "$@"
  else fail "没有找到 docker compose，请先安装 Docker Desktop 或 docker-compose-plugin。"
  fi
}

need_docker() {
  command -v docker >/dev/null 2>&1 || fail "没有找到 docker，请先安装 Docker（或用 ./start.sh local 直接运行）。"
  docker info >/dev/null 2>&1 || fail "Docker 没有在运行，请先启动 Docker。"
}

wait_ready() {
  say "等待服务就绪…"
  for _ in $(seq 1 60); do
    if curl -fsS "${URL}/api/health" >/dev/null 2>&1; then
      say "已启动：${URL}"
      [ -n "${APP_PASSWORD:-}" ] && echo "    已开启访问口令（用户名随意，密码为 APP_PASSWORD）"
      echo "    第一次使用请先到「模型设置」填写模型名称、接口地址和 API Key。"
      return 0
    fi
    sleep 1
  done
  return 1
}

case "${1:-start}" in
  start|up)
    need_docker
    say "构建镜像并启动（第一次会比较慢）…"
    if [ -n "${PIP_INDEX_URL:-}" ]; then
      compose build --build-arg "PIP_INDEX_URL=${PIP_INDEX_URL}"
    else
      compose build
    fi
    compose up -d
    wait_ready || { compose logs --tail 40; fail "服务没有在 60 秒内就绪，上面是最近的日志。"; }
    ;;
  stop|down)
    need_docker; compose down; say "已停止（数据保留在 grader-data 卷里）。" ;;
  restart)
    "$0" stop; "$0" start ;;
  logs)
    need_docker; compose logs -f --tail 100 ;;
  status)
    need_docker; compose ps
    curl -fsS "${URL}/api/health" >/dev/null 2>&1 && say "服务正常：${URL}" || echo "服务没有响应：${URL}" ;;
  local)
    PY="${PYTHON:-python3}"
    command -v "$PY" >/dev/null 2>&1 || fail "没有找到 ${PY}。"
    "$PY" -c 'import sys; sys.exit(0 if sys.version_info >= (3, 10) else 1)' || fail "需要 Python 3.10 或更高版本。"
    if [ ! -d .venv ]; then say "创建虚拟环境 .venv …"; "$PY" -m venv .venv; fi
    say "安装依赖…"
    .venv/bin/pip install -q ${PIP_INDEX_URL:+-i "$PIP_INDEX_URL"} -r backend/requirements.txt
    export DATA_DIR="${DATA_DIR:-$(pwd)/data}"
    say "启动：${URL}　（数据目录 ${DATA_DIR}，Ctrl+C 停止）"
    cd backend && exec ../.venv/bin/uvicorn app.main:app --host "${HOST:-127.0.0.1}" --port "${PORT}"
    ;;
  *)
    sed -n '2,16p' "$0" | sed 's/^# \{0,1\}//'; exit 1 ;;
esac
