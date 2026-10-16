#!/usr/bin/env bash
set -Eeuo pipefail

PORT=1212
ORIGINAL_ARGS=("$@")
SERVICE_NAME=gztv5-customizer
ENV_FILE=/etc/gztv5-customizer.env
UNIT_FILE=/etc/systemd/system/gztv5-customizer.service

usage() {
  cat <<'USAGE'
用法：sudo bash deploy/install.sh [--port 1212]

在 Ubuntu 24.04+ 上安装 APK 工具链与 Python 依赖，创建受资源限制的
systemd 服务。默认公开免登录，监听 0.0.0.0:1212。
运行前须自行安全放入 private/signing.p12 和 private/signing.pass。
USAGE
}

die() {
  printf '错误：%s\n' "$*" >&2
  exit 1
}

while (($#)); do
  case "$1" in
    --port)
      (($# >= 2)) || die '--port 后必须指定端口。'
      PORT=$2
      shift 2
      ;;
    -h|--help)
      usage
      exit 0
      ;;
    *)
      die "未知参数：$1（使用 --help 查看用法）"
      ;;
  esac
done

[[ "$PORT" =~ ^[0-9]{1,5}$ ]] || die '端口必须是 1–65535 的数字。'
PORT_NUM=$((10#$PORT))
((PORT_NUM >= 1 && PORT_NUM <= 65535)) || die '端口必须在 1–65535 范围内。'
PORT=$PORT_NUM

SCRIPT_PATH="$(readlink -f -- "${BASH_SOURCE[0]}")"
APP_DIR="$(cd -- "$(dirname -- "$SCRIPT_PATH")/.." && pwd -P)"
[[ "$APP_DIR" =~ ^/opt/[A-Za-z0-9_./-]+$ ]] || \
  die '请先将项目解压到 /opt 下不含空格的目录，例如 /opt/GZTV5_customizer_web。'

if (( EUID != 0 )); then
  command -v sudo >/dev/null 2>&1 || die '需要 root 权限；请安装 sudo 或以 root 运行。'
  exec sudo -- bash "$SCRIPT_PATH" "${ORIGINAL_ARGS[@]}"
fi

[[ -r /etc/os-release ]] || die '无法识别操作系统。'
# shellcheck disable=SC1091
. /etc/os-release
[[ "${ID:-}" == ubuntu ]] || die '一键脚本目前面向 Ubuntu 24.04+。'
dpkg --compare-versions "${VERSION_ID:-0}" ge 24.04 || die '需要 Ubuntu 24.04 或更新版本。'
[[ -d /run/systemd/system ]] || die '未检测到 systemd；此脚本需要 systemd 主机。'

BASE_APK="$APP_DIR/private/base.apk"
KEYSTORE="$APP_DIR/private/signing.p12"
PASSFILE="$APP_DIR/private/signing.pass"
[[ -s "$BASE_APK" ]] || die "缺少随包基准 APK：$BASE_APK"
[[ -s "$KEYSTORE" ]] || die "缺少签名证书：$KEYSTORE。请通过安全渠道自行放入。"
[[ -s "$PASSFILE" ]] || die "缺少签名口令文件：$PASSFILE。请通过安全渠道自行放入。"

printf '安装系统依赖（APK 工具链、Java、Python、curl）……\n'
export DEBIAN_FRONTEND=noninteractive
apt-get update
apt-get install -y --no-install-recommends \
  apktool aapt apksigner zipalign unzip libsmali-java \
  openjdk-17-jre-headless python3 python3-venv \
  ca-certificates curl openssl iproute2

for tool in apktool aapt apksigner zipalign smali java unzip curl; do
  command -v "$tool" >/dev/null 2>&1 || die "安装后仍找不到工具：$tool"
done

getent group webapp >/dev/null 2>&1 || groupadd --system webapp
if ! getent passwd webapp >/dev/null 2>&1; then
  useradd --system --gid webapp --no-create-home \
    --home-dir /nonexistent --shell /usr/sbin/nologin webapp
fi

printf '安装 Python 虚拟环境与应用依赖……\n'
python3 -m venv "$APP_DIR/.venv"
"$APP_DIR/.venv/bin/python" -m pip install --disable-pip-version-check --no-cache-dir \
  -r "$APP_DIR/requirements.txt"

install -d -o webapp -g webapp -m 0700 \
  "$APP_DIR/runtime" "$APP_DIR/runtime/jobs" \
  "$APP_DIR/runtime/tmp" "$APP_DIR/runtime/home"
chown root:webapp "$APP_DIR" "$APP_DIR/private"
chmod 0750 "$APP_DIR" "$APP_DIR/private"
for file in "$APP_DIR/webapp.py" "$APP_DIR/customize_gztv5.py" "$APP_DIR/index.html"; do
  chown root:webapp "$file"
  chmod 0640 "$file"
done
chown root:webapp "$BASE_APK" "$KEYSTORE" "$PASSFILE"
chmod 0440 "$BASE_APK" "$KEYSTORE" "$PASSFILE"

if [[ -e "$ENV_FILE" ]]; then
  for expected in \
    "GZTV5_BASE_APK=$BASE_APK" \
    "GZTV5_KEYSTORE=$KEYSTORE" \
    "GZTV5_PASSFILE=$PASSFILE" \
    "GZTV5_RUNTIME_DIR=$APP_DIR/runtime"; do
    grep -Fxq -- "$expected" "$ENV_FILE" || \
      die "$ENV_FILE 已存在但路径与当前安装目录不同；请先备份并手动核对配置，脚本不会覆盖它。"
  done
  grep -Eq '^APP_SECRET_KEY=.{32,}$' "$ENV_FILE" || \
    die "$ENV_FILE 已存在但缺少有效 APP_SECRET_KEY；请手动修复后重试。"
  printf '保留已有 %s（不会覆盖现有密钥）。\n' "$ENV_FILE"
else
  app_secret_key="$(openssl rand -hex 32)"
  (
    umask 077
    {
      printf 'PUBLIC_MODE=1\nCOOKIE_SECURE=0\nAPP_SECRET_KEY=%s\n' "$app_secret_key"
      printf 'GZTV5_BASE_APK=%s\nGZTV5_KEYSTORE=%s\nGZTV5_PASSFILE=%s\n' \
        "$BASE_APK" "$KEYSTORE" "$PASSFILE"
      printf 'GZTV5_RUNTIME_DIR=%s/runtime\nTMPDIR=%s/runtime/tmp\nHOME=%s/runtime/home\n' \
        "$APP_DIR" "$APP_DIR" "$APP_DIR"
      printf 'PYTHONDONTWRITEBYTECODE=1\n'
      printf 'JAVA_TOOL_OPTIONS="-XX:ActiveProcessorCount=1 -Xms32m -Xmx288m -XX:+UseSerialGC -Djava.io.tmpdir=%s/runtime/tmp"\n' \
        "$APP_DIR"
    } > "$ENV_FILE"
  )
  unset app_secret_key
  chown root:root "$ENV_FILE"
  chmod 0600 "$ENV_FILE"
fi

if ! systemctl is-active --quiet "$SERVICE_NAME" && \
   ss -H -ltn "sport = :$PORT" | grep -q .; then
  die "端口 $PORT 已被其他服务占用；请释放端口或使用 --port 指定另一个端口。"
fi

cat > "$UNIT_FILE" <<UNIT
[Unit]
Description=GZTV5 APK Customizer (public, resource-limited)
Wants=network-online.target
After=network-online.target

[Service]
Type=simple
User=webapp
Group=webapp
WorkingDirectory=$APP_DIR
EnvironmentFile=$ENV_FILE
ExecStart=$APP_DIR/.venv/bin/uvicorn webapp:app --host 0.0.0.0 --port $PORT --workers 1
Restart=on-failure
RestartSec=5s
TimeoutStopSec=20s
KillSignal=SIGTERM
Nice=10
CPUQuota=75%
MemoryMax=448M
MemorySwapMax=0
OOMScoreAdjust=500
TasksMax=128
UMask=0077
NoNewPrivileges=true
ProtectSystem=strict
ProtectHome=true
PrivateTmp=true
ReadWritePaths=$APP_DIR/runtime
ReadOnlyPaths=$APP_DIR/private
ProtectKernelTunables=true
ProtectKernelModules=true
ProtectControlGroups=true
RestrictSUIDSGID=true
LockPersonality=true

[Install]
WantedBy=multi-user.target
UNIT
chmod 0644 "$UNIT_FILE"
systemd-analyze verify "$UNIT_FILE"
systemctl daemon-reload
systemctl enable "$SERVICE_NAME.service" >/dev/null
systemctl restart "$SERVICE_NAME.service"

printf '检查本机健康状态……\n'
healthy=0
for _ in $(seq 1 30); do
  if curl --noproxy '*' -fsS --connect-timeout 2 --max-time 3 \
      "http://127.0.0.1:$PORT/healthz" >/dev/null; then
    healthy=1
    break
  fi
  sleep 1
done
if (( healthy == 0 )); then
  journalctl -u "$SERVICE_NAME.service" -n 80 --no-pager >&2 || true
  die '服务未通过健康检查；请查看上方日志。'
fi

if command -v ufw >/dev/null 2>&1 && ufw status 2>/dev/null | grep -q '^Status: active'; then
  ufw allow "$PORT/tcp" >/dev/null
  printf '已在活动的 UFW 防火墙中放行 TCP %s。\n' "$PORT"
fi

printf '\n部署完成。\n'
printf '访问地址：http://<服务器公网 IP>:%s/\n' "$PORT"
printf '健康检查：http://127.0.0.1:%s/healthz\n' "$PORT"
printf '服务状态：systemctl status %s.service\n' "$SERVICE_NAME"
printf '日志查看：journalctl -u %s.service -f\n' "$SERVICE_NAME"
printf '注意：当前为公开免登录模式；如使用云安全组，请另行开放 TCP %s。\n' "$PORT"
