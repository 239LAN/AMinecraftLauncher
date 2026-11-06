#!/usr/bin/env bash
# AML relay 一键部署脚本（Linux 服务器，需 root 运行）。
#
# 用法：
#   ./deploy.sh --role all             # 单实例同时部署主节点 + 子节点（推荐新服务器）
#   ./deploy.sh --role node            # 只部署子节点（监听 3375）
#   ./deploy.sh --role master          # 只部署主节点（监听 3380）
#   AML_ROLE=node ./deploy.sh           # 也可以用环境变量选择角色
#
# 单实例双角色时：
#   主节点 aml-relay-master.service（3380）读取 /etc/aml-relay/master.env
#   子节点 aml-relay-node.service  （3375）读取 /etc/aml-relay/node.env
#   frps 仍为 frps.service（3300），节点注册表 /etc/aml-relay/nodes.json
#
# 可选环境变量：
#   FRP_VERSION      frps 版本，默认 0.72.0（需与客户端 frpc 一致）
#   FRP_PACKAGE      本地 frp 压缩包路径；不填时自动使用 relay/ 或项目根目录下的
#                    frp_*_linux_<arch>.tar.gz（存在即优先于在线下载）
#   FRPS_URL         自定义 frps 压缩包下载地址（优先于上面的公共加速源）
#   FRP_FORCE        设为 1 可强制重装已存在的 frps
#   GH_PROXY         GitHub 加速前缀，例如 https://ghfast.top/
#   AML_NODE_ID / AML_NODE_NAME / AML_REGION / AML_PUBLIC_API_URL
#   AML_MASTER_API_PORT / AML_NODE_API_PORT（仅 --role all）
#   AML_FRPS_ADDR / AML_FRPS_PORT / AML_FRPS_TOKEN
#   AML_NODES_FILE / AML_NODES_JSON（主节点节点注册表）
#
# 脚本可重复执行：只会更新代码、配置与 systemd 单元。

set -euo pipefail

APP_DIR=/opt/aml-relay
CONF_DIR=/etc/aml-relay
DATA_DIR=/var/lib/aml-relay
LOG_DIR=/var/log/aml-relay
ENV_FILE="$CONF_DIR/relay.env"
MASTER_ENV_FILE="$CONF_DIR/master.env"
NODE_ENV_FILE="$CONF_DIR/node.env"
FRPS_CONFIG="$CONF_DIR/frps.toml"
SERVICE_NAME=aml-relay.service
MASTER_SERVICE=aml-relay-master.service
NODE_SERVICE=aml-relay-node.service
FRPS_SERVICE=frps.service

SRC_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
FRP_VERSION="${FRP_VERSION:-0.72.0}"
GH_PROXY="${GH_PROXY:-}"

usage() {
    cat <<'EOF'
用法：deploy.sh --role <all|master|node>

  --role all      单实例：同时部署主节点（3380）与子节点（3375）及 frps
  --role master   主节点：只提供节点列表（默认 3380）
  --role node     子节点：提供鉴权、房间与隧道配置（默认 3375）
  -h, --help      显示本帮助
EOF
}

log() { printf '\033[1;32m[aml-relay]\033[0m %s\n' "$*"; }
warn() { printf '\033[1;33m[aml-relay]\033[0m %s\n' "$*"; }
fail() { printf '\033[1;31m[aml-relay]\033[0m %s\n' "$*" >&2; exit 1; }

ROLE="${AML_ROLE:-node}"
while [[ $# -gt 0 ]]; do
    case "$1" in
        --role)
            [[ $# -ge 2 ]] || fail "参数 --role 需要一个值（all / master / node）"
            ROLE="$2"
            shift 2
            ;;
        --role=*)
            ROLE="${1#*=}"
            shift
            ;;
        -h|--help)
            usage
            exit 0
            ;;
        *)
            fail "未知参数：$1（可用 --role all|master|node）"
            ;;
    esac
done
ROLE="$(printf '%s' "$ROLE" | tr '[:upper:]' '[:lower:]')"
case "$ROLE" in
    all|master|node) ;;
    *) fail "AML_ROLE 只能是 all / master / node，当前为：$ROLE" ;;
esac

[[ "${EUID:-$(id -u)}" -eq 0 ]] || fail "请使用 root 运行本脚本（sudo ./deploy.sh --role $ROLE）"
[[ -d "$SRC_DIR" && -f "$SRC_DIR/requirements.txt" ]] || fail "未找到 relay 源码目录，请在 relay/ 下执行脚本"

if [[ "$ROLE" == "master" ]]; then
    DEFAULT_API_PORT=3380
else
    DEFAULT_API_PORT=3375
fi
API_PORT="${AML_API_PORT:-$DEFAULT_API_PORT}"
FRPS_PORT="${AML_FRPS_PORT:-3300}"

# ---- 系统依赖 ----

log "安装系统依赖（python3-venv / curl / tar）"
command -v apt-get >/dev/null 2>&1 || fail "当前系统没有 apt-get，请手动安装 python3、python3-venv、curl 后重试"
apt-get update -y
apt-get install -y python3 python3-venv python3-pip curl tar
command -v systemctl >/dev/null 2>&1 || fail "未检测到 systemd，请手动启动 relay 服务"

# ---- 目录 ----

install -d -m 0755 "$APP_DIR/app" "$APP_DIR/bin"
install -d -m 0755 "$CONF_DIR"
install -d -m 0755 "$DATA_DIR" "$LOG_DIR"

# ---- 代码与依赖 ----

log "安装 relay 源码到 $APP_DIR/app/relay"
rm -rf "$APP_DIR/app/relay"
cp -r "$SRC_DIR" "$APP_DIR/app/relay"

if [[ ! -x "$APP_DIR/venv/bin/python" ]]; then
    log "创建 Python 虚拟环境"
    python3 -m venv "$APP_DIR/venv"
fi
log "安装 Python 依赖"
"$APP_DIR/venv/bin/python" -m pip install --upgrade pip >/dev/null
"$APP_DIR/venv/bin/python" -m pip install -r "$APP_DIR/app/relay/requirements.txt"

# ---- frps 二进制 ----

install_frps() {
    local arch base tmp pkg candidate ok local_pkg
    if [[ -x "$APP_DIR/bin/frps" && "${FRP_FORCE:-0}" != "1" ]]; then
        log "frps 已存在，跳过下载（FRP_FORCE=1 可强制重装）"
        return 0
    fi
    case "$(uname -m)" in
        x86_64|amd64) arch=amd64 ;;
        aarch64|arm64) arch=arm64 ;;
        *) fail "暂不支持的 CPU 架构：$(uname -m)，请手动下载 frps 放到 $APP_DIR/bin/frps" ;;
    esac

    # 优先使用仓库目录里的本地 frp 压缩包，避免服务器访问 GitHub。
    local_pkg=""
    for candidate in "${FRP_PACKAGE:-}" \
        "$SRC_DIR/frp_${FRP_VERSION}_linux_${arch}.tar.gz" \
        "$SRC_DIR/../frp_${FRP_VERSION}_linux_${arch}.tar.gz" \
        "$SRC_DIR"/frp_*_linux_"${arch}".tar.gz \
        "$SRC_DIR"/../frp_*_linux_"${arch}".tar.gz; do
        if [[ -n "$candidate" && -f "$candidate" ]]; then
            local_pkg="$candidate"
            break
        fi
    done

    tmp="$(mktemp -d)"
    if [[ -n "$local_pkg" ]]; then
        log "使用本地 frp 安装包：$local_pkg"
        tar -xzf "$local_pkg" -C "$tmp" || fail "本地安装包解压失败：$local_pkg"
    else
        base="https://github.com/fatedier/frp/releases/download/v${FRP_VERSION}/frp_${FRP_VERSION}_linux_${arch}.tar.gz"
        local candidates=()
        if [[ -n "${FRPS_URL:-}" ]]; then
            candidates=("$FRPS_URL")
        else
            if [[ -n "$GH_PROXY" ]]; then
                candidates+=("${GH_PROXY}${base}")
            fi
            candidates+=(
                "https://v6.gh-proxy.org/${base}"
                "https://ghfast.top/${base}"
                "https://gh-proxy.com/${base}"
                "$base"
            )
        fi

        ok=0
        for candidate in "${candidates[@]}"; do
            log "下载 frps v${FRP_VERSION}（${arch}）：$candidate"
            if curl -fL --retry 2 --connect-timeout 10 --max-time 300 \
                -o "$tmp/frp.tar.gz" "$candidate"; then
                ok=1
                break
            fi
            warn "该源下载失败，尝试下一个"
        done
        if [[ "$ok" != "1" ]]; then
            rm -rf "$tmp"
            warn "frps 所有下载源均失败。可手动上传安装包后重跑："
            warn "  FRPS_URL=https://你的地址/frp_${FRP_VERSION}_linux_${arch}.tar.gz ./deploy.sh --role $ROLE"
            warn "或在本机下载后上传到服务器："
            warn "  https://github.com/fatedier/frp/releases/download/v${FRP_VERSION}/frp_${FRP_VERSION}_linux_${arch}.tar.gz"
            warn "  解压后把 frps 放到 $APP_DIR/bin/frps 并 chmod +x，然后重跑脚本（会跳过下载）。"
            exit 1
        fi
        tar -xzf "$tmp/frp.tar.gz" -C "$tmp" || fail "frps 压缩包解压失败"
    fi

    pkg="$(find "$tmp" -maxdepth 2 -type f -name frps -print -quit)"
    [[ -n "$pkg" ]] || fail "安装包内未找到 frps，请检查压缩包"
    install -m 0755 "$pkg" "$APP_DIR/bin/frps"
    rm -rf "$tmp"
    log "frps 安装完成：$APP_DIR/bin/frps"
}
if [[ "$ROLE" == "node" || "$ROLE" == "all" ]]; then
    install_frps
fi

# ---- 环境变量文件 ----

read_env_file() {
    local file="$1" key="$2" line
    [[ -f "$file" ]] || return 0
    line="$(grep -E "^${key}=" "$file" | tail -n 1 || true)"
    printf '%s' "${line#*=}"
}

read_env() { read_env_file "$ENV_FILE" "$1"; }

env_upsert_file() {
    local file="$1" key="$2" value="$3" tmp
    [[ -z "$value" ]] && return 0
    [[ -f "$file" ]] || : > "$file"
    if grep -qE "^${key}=" "$file"; then
        tmp="${file}.tmp"
        awk -v key="$key" -v val="$value" '
            BEGIN { FS = OFS = "=" }
            $1 == key { print key "=" val; next }
            { print }
        ' "$file" > "$tmp"
        mv "$tmp" "$file"
    else
        printf '%s=%s\n' "$key" "$value" >> "$file"
    fi
    chmod 600 "$file"
}

env_upsert() { env_upsert_file "$ENV_FILE" "$1" "$2"; }

# 子节点（含 all 模式）需要告诉客户端 frps 的公网地址；未显式指定时自动探测。
FRPS_PUBLIC_ADDR="${AML_FRPS_ADDR:-}"
if [[ "$ROLE" == "node" || "$ROLE" == "all" ]] && [[ -z "$FRPS_PUBLIC_ADDR" ]]; then
    FRPS_PUBLIC_ADDR="$(curl -fsS --max-time 6 https://api.ipify.org 2>/dev/null || true)"
    [[ -n "$FRPS_PUBLIC_ADDR" ]] || FRPS_PUBLIC_ADDR="$(hostname -I 2>/dev/null | awk '{print $1}')"
fi
FRPS_PUBLIC_ADDR="${FRPS_PUBLIC_ADDR:-127.0.0.1}"

write_env_template() {
    local file="$1" role="$2" port="$3" nodes_file="${4:-}"
    [[ -f "$file" ]] && return 0
    log "生成 $file"
    cat > "$file" <<EOF
# AML relay 运行配置（deploy.sh 生成，可自行调整后 systemctl restart 对应服务）
AML_ROLE=$role
AML_API_PORT=$port
AML_NODE_ID=${AML_NODE_ID:-node-1}
AML_NODE_NAME=${AML_NODE_NAME:-AML 节点}
AML_REGION=${AML_REGION:-cn}
AML_PUBLIC_API_URL=${AML_PUBLIC_API_URL:-}
AML_FRPS_ADDR=$FRPS_PUBLIC_ADDR
AML_FRPS_PORT=$FRPS_PORT
AML_FRPS_TOKEN=${AML_FRPS_TOKEN:-}
AML_DB_PATH=$DATA_DIR/relay.db
AML_ROOM_TTL=${AML_ROOM_TTL:-7200}
AML_HEARTBEAT_TIMEOUT=${AML_HEARTBEAT_TIMEOUT:-45}
AML_MAX_ROOMS_PER_HOST=${AML_MAX_ROOMS_PER_HOST:-1}
AML_MAX_ROOMS_PER_IP=${AML_MAX_ROOMS_PER_IP:-1}
AML_MAX_GUESTS=${AML_MAX_GUESTS:-16}
EOF
    if [[ -n "$nodes_file" ]]; then
        printf 'AML_NODES_FILE=%s\n' "$nodes_file" >> "$file"
    fi
    chmod 600 "$file"
}

if [[ "$ROLE" == "all" ]]; then
    # 旧版单角色部署：把 relay.env 的设置迁到 node.env，避免重新生成 token。
    if [[ -f "$ENV_FILE" && ! -f "$NODE_ENV_FILE" ]]; then
        cp "$ENV_FILE" "$NODE_ENV_FILE"
        env_upsert_file "$NODE_ENV_FILE" AML_ROLE node
        # 旧版若以主节点部署，端口可能是 3380，这里强制回子节点端口避免冲突。
        env_upsert_file "$NODE_ENV_FILE" AML_API_PORT "${AML_NODE_API_PORT:-3375}"
        chmod 600 "$NODE_ENV_FILE"
        warn "已把旧配置 $ENV_FILE 迁移到 $NODE_ENV_FILE"
    fi
fi

MASTER_API_PORT="$(read_env_file "$MASTER_ENV_FILE" AML_API_PORT)"
MASTER_API_PORT="${AML_MASTER_API_PORT:-${MASTER_API_PORT:-3380}}"
NODE_API_PORT="$(read_env_file "$NODE_ENV_FILE" AML_API_PORT)"
NODE_API_PORT="${AML_NODE_API_PORT:-${NODE_API_PORT:-3375}}"

if [[ "$ROLE" == "all" ]]; then
    write_env_template "$NODE_ENV_FILE" node "$NODE_API_PORT"
    write_env_template "$MASTER_ENV_FILE" master "$MASTER_API_PORT" "$CONF_DIR/nodes.json"

    for key in AML_NODE_ID AML_NODE_NAME AML_REGION AML_PUBLIC_API_URL \
               AML_FRPS_ADDR AML_FRPS_PORT AML_FRPS_TOKEN AML_ROOM_TTL \
               AML_HEARTBEAT_TIMEOUT AML_MAX_ROOMS_PER_HOST AML_MAX_ROOMS_PER_IP AML_MAX_GUESTS; do
        env_upsert_file "$NODE_ENV_FILE" "$key" "$(printenv "$key" || true)"
    done
    env_upsert_file "$NODE_ENV_FILE" AML_ROLE node
    env_upsert_file "$NODE_ENV_FILE" AML_API_PORT "$NODE_API_PORT"
    env_upsert_file "$NODE_ENV_FILE" AML_DB_PATH "$DATA_DIR/relay.db"

    env_upsert_file "$MASTER_ENV_FILE" AML_ROLE master
    env_upsert_file "$MASTER_ENV_FILE" AML_API_PORT "$MASTER_API_PORT"
    env_upsert_file "$MASTER_ENV_FILE" AML_NODES_FILE "$CONF_DIR/nodes.json"

    # 主节点节点注册表：首次部署（或旧文件为空）时自动登记本机子节点。
    if [[ -f "$CONF_DIR/nodes.json" ]] && grep -q '"api"' "$CONF_DIR/nodes.json"; then
        log "已保留现有节点注册表 $CONF_DIR/nodes.json"
    else
        NODE_ID="$(read_env_file "$NODE_ENV_FILE" AML_NODE_ID)"
        NODE_NAME="$(read_env_file "$NODE_ENV_FILE" AML_NODE_NAME)"
        NODE_REGION="$(read_env_file "$NODE_ENV_FILE" AML_REGION)"
        NODE_PUBLIC_API="$(read_env_file "$NODE_ENV_FILE" AML_PUBLIC_API_URL)"
        NODE_PUBLIC_API="${NODE_PUBLIC_API:-http://127.0.0.1:$NODE_API_PORT}"
        python3 - "$CONF_DIR/nodes.json" "$NODE_ID" "$NODE_NAME" "$NODE_REGION" "$NODE_PUBLIC_API" <<'PY'
import json
import sys

path, node_id, name, region, api = sys.argv[1:6]
with open(path, "w", encoding="utf-8") as fh:
    json.dump([{"id": node_id, "name": name, "region": region, "api": api}],
              fh, ensure_ascii=False, indent=2)
    fh.write("\n")
PY
        chmod 644 "$CONF_DIR/nodes.json"
        log "已生成本机节点注册表 $CONF_DIR/nodes.json"
    fi
else
    NODES_ARG=""
    if [[ "$ROLE" == "master" ]]; then
        NODES_ARG="$CONF_DIR/nodes.json"
    fi
    write_env_template "$ENV_FILE" "$ROLE" "$API_PORT" "$NODES_ARG"

    # 显式传入的环境变量覆盖配置文件中的同名项。
    for key in AML_API_PORT AML_NODE_ID AML_NODE_NAME AML_REGION AML_PUBLIC_API_URL \
               AML_FRPS_ADDR AML_FRPS_PORT AML_FRPS_TOKEN AML_ROOM_TTL AML_HEARTBEAT_TIMEOUT \
               AML_MAX_ROOMS_PER_HOST AML_MAX_ROOMS_PER_IP AML_MAX_GUESTS AML_NODES_FILE; do
        env_upsert "$key" "$(printenv "$key" || true)"
    done
    env_upsert AML_ROLE "$ROLE"
    API_PORT="$(read_env AML_API_PORT)"
    API_PORT="${API_PORT:-$DEFAULT_API_PORT}"
    env_upsert AML_API_PORT "$API_PORT"

    # 主节点节点注册表不存在时给一个空数组，避免启动即报错。
    if [[ "$ROLE" == "master" && ! -f "$CONF_DIR/nodes.json" ]]; then
        printf '[]\n' > "$CONF_DIR/nodes.json"
    fi
fi

if [[ "$ROLE" == "all" ]]; then
    ENV_SOURCE="$NODE_ENV_FILE"
else
    ENV_SOURCE="$ENV_FILE"
fi
FRPS_PORT="$(read_env_file "$ENV_SOURCE" AML_FRPS_PORT)"
FRPS_PORT="${FRPS_PORT:-3300}"
FRPS_TOKEN="$(read_env_file "$ENV_SOURCE" AML_FRPS_TOKEN)"
if [[ -z "$FRPS_TOKEN" ]]; then
    FRPS_TOKEN="$(openssl rand -hex 24 2>/dev/null || python3 -c 'import secrets; print(secrets.token_hex(24))')"
    if [[ "$ROLE" == "all" ]]; then
        env_upsert_file "$NODE_ENV_FILE" AML_FRPS_TOKEN "$FRPS_TOKEN"
    else
        env_upsert AML_FRPS_TOKEN "$FRPS_TOKEN"
    fi
fi
if [[ "$ROLE" == "all" ]]; then
    env_upsert_file "$NODE_ENV_FILE" AML_FRPS_PORT "$FRPS_PORT"
else
    env_upsert AML_DB_PATH "$DATA_DIR/relay.db"
fi

# ---- frps 配置（子节点 / 单实例双角色需要） ----

if [[ "$ROLE" == "node" || "$ROLE" == "all" ]]; then
log "生成 $FRPS_CONFIG"
cat > "$FRPS_CONFIG" <<EOF
# AML relay 使用的 frps 配置（deploy.sh 生成）
bindAddr = "0.0.0.0"
bindPort = $FRPS_PORT

# 必须与 node.env 中 AML_FRPS_TOKEN 一致
auth.token = "$FRPS_TOKEN"

# 强制 TLS，避免明文传输隧道凭据
transport.tls.force = true

# 限制每个客户端最多注册 2 条代理，降低凭据被滥用后的影响
maxPortsPerClient = 2

# 只允许极小的 TCP 端口段；STCP 联机不使用远程端口，
# 这样可以阻止访客用泄露的 token 把其他 TCP 服务穿透到公网。
allowPorts = [{ start = 30000, end = 30010 }]

# 仅本机可访问的管理台
webServer.addr = "127.0.0.1"
webServer.port = 7500
webServer.user = "admin"
webServer.password = "aml-dashboard"

log.to = "$LOG_DIR/frps.log"
log.level = "info"
log.maxDays = 7
EOF
chmod 600 "$FRPS_CONFIG"
fi

# ---- systemd 单元 ----

write_relay_unit() {
    local name="$1" env_file="$2" role="$3"
    cat > "/etc/systemd/system/$name" <<EOF
[Unit]
Description=AML Relay API（Minecraft 联机房间服务，角色：$role）
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
EnvironmentFile=$env_file
WorkingDirectory=$APP_DIR/app
ExecStart=$APP_DIR/venv/bin/python -m relay.main
Restart=always
RestartSec=5
User=root

[Install]
WantedBy=multi-user.target
EOF
}

log "写入 systemd 单元"
if [[ "$ROLE" == "all" ]]; then
    write_relay_unit "$MASTER_SERVICE" "$MASTER_ENV_FILE" master
    write_relay_unit "$NODE_SERVICE" "$NODE_ENV_FILE" node
    # 旧版单角色服务与新服务端口冲突，先停用并移除。
    if [[ -f "/etc/systemd/system/$SERVICE_NAME" ]]; then
        systemctl disable --now "$SERVICE_NAME" >/dev/null 2>&1 || true
        rm -f "/etc/systemd/system/$SERVICE_NAME"
        warn "已停用旧版单角色服务 $SERVICE_NAME（改用 $MASTER_SERVICE / $NODE_SERVICE）"
    fi
else
    write_relay_unit "$SERVICE_NAME" "$ENV_FILE" "$ROLE"
fi

if [[ "$ROLE" == "node" || "$ROLE" == "all" ]]; then
cat > "/etc/systemd/system/$FRPS_SERVICE" <<EOF
[Unit]
Description=frps（AML relay 隧道服务端）
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
WorkingDirectory=$CONF_DIR
ExecStart=$APP_DIR/bin/frps -c $FRPS_CONFIG
Restart=always
RestartSec=5
User=root

[Install]
WantedBy=multi-user.target
EOF
fi

systemctl daemon-reload
if [[ "$ROLE" == "all" ]]; then
    systemctl enable "$MASTER_SERVICE" "$NODE_SERVICE" "$FRPS_SERVICE" >/dev/null
    log "启动 frps"
    systemctl restart "$FRPS_SERVICE" || warn "frps 启动失败，请查看：journalctl -u $FRPS_SERVICE -n 50"
    log "启动 $MASTER_SERVICE"
    systemctl restart "$MASTER_SERVICE" || warn "$MASTER_SERVICE 启动失败，请查看：journalctl -u $MASTER_SERVICE -n 50"
    log "启动 $NODE_SERVICE"
    systemctl restart "$NODE_SERVICE" || warn "$NODE_SERVICE 启动失败，请查看：journalctl -u $NODE_SERVICE -n 50"
else
    systemctl enable "$SERVICE_NAME" >/dev/null
    if [[ "$ROLE" == "node" ]]; then
        systemctl enable "$FRPS_SERVICE" >/dev/null
        log "启动 frps"
        systemctl restart "$FRPS_SERVICE" || warn "frps 启动失败，请查看：journalctl -u $FRPS_SERVICE -n 50"
    fi
    log "启动 aml-relay"
    systemctl restart "$SERVICE_NAME" || warn "aml-relay 启动失败，请查看：journalctl -u $SERVICE_NAME -n 50"
fi

# ---- 结果 ----

if [[ "$ROLE" == "all" ]]; then
    NODE_PUBLIC_API="$(read_env_file "$NODE_ENV_FILE" AML_PUBLIC_API_URL)"
    NODE_PUBLIC_API="${NODE_PUBLIC_API:-http://127.0.0.1:$NODE_API_PORT}"
    cat <<EOF

部署完成（单实例：主节点 + 子节点）。
  主节点 API : $MASTER_API_PORT（/api/v1/nodes）
  子节点 API : $NODE_API_PORT（鉴权 / 房间 / meta）
  frps 端口  : $FRPS_PORT（管理台 127.0.0.1:7500）
  配置文件   : $MASTER_ENV_FILE
               $NODE_ENV_FILE
  节点注册表 : $CONF_DIR/nodes.json
  服务       : $MASTER_SERVICE / $NODE_SERVICE / $FRPS_SERVICE

自检：
  curl http://127.0.0.1:$MASTER_API_PORT/api/v1/nodes
  curl http://127.0.0.1:$NODE_API_PORT/healthz
  cat $CONF_DIR/nodes.json

启动器里把主节点地址填为反代到 $MASTER_API_PORT 的公网地址；
  节点 api 必须能被客户端访问，当前为：$NODE_PUBLIC_API
EOF
    if [[ "$NODE_PUBLIC_API" == http://127.0.0.1* || "$NODE_PUBLIC_API" == http://localhost* ]]; then
        warn "nodes.json 里的节点 api 仍是本机地址，外部客户端无法连接。"
        warn "请设置 AML_PUBLIC_API_URL=https://你的联机域名:$NODE_API_PORT 后重跑本脚本，"
        warn "或手动修改 $CONF_DIR/nodes.json 里的 api 字段。"
    fi
    cat <<EOF
  systemctl status $MASTER_SERVICE --no-pager
  systemctl status $NODE_SERVICE --no-pager
  journalctl -u $NODE_SERVICE -n 50
EOF
    exit 0
fi

cat <<EOF

部署完成（$ROLE）。
  API 监听端口 : $API_PORT
EOF
if [[ "$ROLE" == "node" ]]; then
    cat <<EOF
  frps 端口    : $FRPS_PORT（管理台 127.0.0.1:7500，账号 admin / aml-dashboard）
EOF
fi
cat <<EOF
  配置文件     : $ENV_FILE
  数据库       : $(read_env AML_DB_PATH)

自检：
  curl http://127.0.0.1:$API_PORT/healthz
EOF
if [[ "$ROLE" == "master" ]]; then
    cat <<EOF
  节点清单     : $CONF_DIR/nodes.json（修改后 systemctl restart aml-relay）
                 示例：[{"id":"node-1","name":"节点 1","region":"cn","api":"https://node1.example.com"}]
  curl http://127.0.0.1:$API_PORT/api/v1/nodes
EOF
else
    cat <<EOF
  公网 frps    : $(read_env AML_FRPS_ADDR):$FRPS_PORT（防火墙需放行该 TCP 端口）
  curl http://127.0.0.1:$API_PORT/api/v1/meta
EOF
fi
cat <<EOF
  systemctl status $SERVICE_NAME --no-pager
  journalctl -u $SERVICE_NAME -n 50
EOF
