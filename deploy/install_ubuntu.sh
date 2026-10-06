#!/usr/bin/env bash
# Instala o ImazonGeo Upload numa VM Ubuntu (22.04 ou 24.04): CLI, simulador
# e a interface web como serviço systemd (gunicorn).
#
# Execute a partir de uma cópia do repositório:
#
#   sudo ./deploy/install_ubuntu.sh
#
# Reexecutar atualiza o código e reinicia o serviço. O arquivo de
# configuração (/etc/imazongeo-upload/imazongeo-upload.env) nunca é
# sobrescrito.
set -euo pipefail

APP_NAME=imazongeo-upload
APP_USER=imazon
APP_DIR=/opt/$APP_NAME
ENV_DIR=/etc/$APP_NAME
ENV_FILE=$ENV_DIR/$APP_NAME.env
STATE_DIR=/var/lib/$APP_NAME
REPO_DIR=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)

if [[ $EUID -ne 0 ]]; then
    echo "Execute como root: sudo $0" >&2
    exit 1
fi

# Primeiro Python 3.10+ com o módulo venv (PYTHON=... força um específico).
# Prefere o mais novo: em máquinas onde o 'python3' foi trocado por uma versão
# própria, o python3.N do sistema costuma ser o que tem venv funcionando.
escolher_python() {
    local candidato
    for candidato in "${PYTHON:-}" python3.13 python3.12 python3.11 python3.10 python3; do
        [[ -n $candidato ]] && command -v "$candidato" >/dev/null 2>&1 || continue
        if "$candidato" -c 'import sys, venv; sys.exit(sys.version_info < (3, 10))' \
            >/dev/null 2>&1; then
            command -v "$candidato"
            return 0
        fi
    done
    return 1
}

echo ">> Python"
if ! PYTHON=$(escolher_python); then
    echo "   Instalando Python pelo apt"
    export DEBIAN_FRONTEND=noninteractive
    # Ganchos quebrados do apt (ex.: cnf-update-db) não devem parar a instalação
    apt-get update -qq || echo "   aviso: 'apt-get update' falhou; seguindo" >&2
    apt-get install -y -qq python3 python3-venv python3-pip >/dev/null
    PYTHON=$(escolher_python) || {
        echo "É necessário Python 3.10+ com o módulo venv." >&2
        exit 1
    }
fi
echo "   usando $PYTHON ($("$PYTHON" -V))"

echo ">> Usuário de serviço ($APP_USER)"
if ! id "$APP_USER" &>/dev/null; then
    useradd --system --home-dir "$STATE_DIR" --shell /usr/sbin/nologin "$APP_USER"
fi

echo ">> Ambiente virtual em $APP_DIR/venv"
install -d -m 755 "$APP_DIR"
"$PYTHON" -m venv "$APP_DIR/venv"
"$APP_DIR/venv/bin/pip" install -q --upgrade pip
# Build a partir de uma cópia, para não deixar build/ e *.egg-info (de root)
# dentro do repositório
BUILD_DIR=$(mktemp -d)
trap 'rm -rf "$BUILD_DIR"' EXIT
cp -r "$REPO_DIR/pyproject.toml" "$REPO_DIR/README.md" "$REPO_DIR/src" "$BUILD_DIR/"
"$APP_DIR/venv/bin/pip" install -q "$BUILD_DIR[web]"
install -m 644 "$REPO_DIR/deploy/gunicorn.conf.py" "$APP_DIR/gunicorn.conf.py"
for cmd in imazongeo-upload imazongeo-simulador imazongeo-banco; do
    ln -sf "$APP_DIR/venv/bin/$cmd" "/usr/local/bin/$cmd"
done

echo ">> Configuração em $ENV_FILE"
install -d -m 750 -o root -g "$APP_USER" "$ENV_DIR"
if [[ ! -f $ENV_FILE ]]; then
    install -m 640 -o root -g "$APP_USER" "$REPO_DIR/.env.example" "$ENV_FILE"
    echo "   Criado a partir de .env.example: preencha as credenciais AWS e"
    echo "   UPLOAD_PASSWORD antes do primeiro upload real."
fi

if [[ -d /run/systemd/system ]]; then
    echo ">> Serviço systemd ($APP_NAME)"
    install -m 644 "$REPO_DIR/deploy/$APP_NAME.service" /etc/systemd/system/
    systemctl daemon-reload
    systemctl enable "$APP_NAME" >/dev/null
    systemctl restart "$APP_NAME"
    systemctl --no-pager --lines=5 status "$APP_NAME" || true
else
    echo ">> systemd não está ativo (container/WSL): serviço não instalado."
    echo "   Para iniciar a interface web manualmente:"
    echo "   set -a; . $ENV_FILE; set +a"
    echo "   $APP_DIR/venv/bin/gunicorn -c $APP_DIR/gunicorn.conf.py" \
        "imazongeo_upload.web.server:app"
fi

cat <<EOF

Instalação concluída.
  - Configuração: sudo nano $ENV_FILE  (depois: sudo systemctl restart $APP_NAME)
  - Interface web: http://127.0.0.1:5000 na VM. De outra máquina, use um túnel
    SSH (ssh -L 5000:127.0.0.1:5000 usuario@vm) ou o proxy nginx de exemplo em
    deploy/nginx-imazongeo-upload.conf.
  - Linha de comando: imazongeo-upload --help
EOF
