#!/usr/bin/env bash
# 构建带自研扩展的部署镜像：server 叠装 wheel、gateway 注入前端；runtime-host 不受扩展影响，
# 把基础标签重打到扩展标签即可（compose 的 KNOWWORK_VERSION 统一引用三个镜像）。
# 用法：scripts/build_ext_images.sh <基础镜像标签> <扩展镜像标签>
#   例：scripts/build_ext_images.sh v0.0.6-mineru41 v0.0.6-mineru41-ext
# 前置：KnowWork 仓库在 ../KnowWork（或 KW_DIR 指定），其 server 侧镜像已按官方
# Dockerfile 构建出基础标签；本机可访问镜像仓库（或已缓存所需基础镜像）。
# 之后的切换：KnowWork deploy/.env 的 KNOWWORK_VERSION 换成扩展标签 → docker compose up -d；
# api 启动时自动升级扩展的迁移分支（memory_migrations），并随 wheel 装上授权校验器。
set -euo pipefail
BASE_TAG="${1:?用法: build_ext_images.sh <基础镜像标签> <扩展镜像标签>}"
EXT_TAG="${2:?用法: build_ext_images.sh <基础镜像标签> <扩展镜像标签>}"
REG="${KNOWWORK_REGISTRY:-crpi-6q638zy09wmnj70w.cn-hangzhou.personal.cr.aliyuncs.com/knowflow-ai}"
ACME_DIR="$(cd "$(dirname "$0")/.." && pwd)"
KW_DIR="${KW_DIR:-$ACME_DIR/../KnowWork}"

echo "== 1/4 构建扩展 wheel =="
(cd "$ACME_DIR/backend" && uv build >/dev/null)
WHEEL=$(ls "$ACME_DIR"/backend/dist/acme_knowwork_enterprise-*.whl | tail -1)
echo "   $WHEEL"

echo "== 2/4 server 叠装镜像 =="
SRV_CTX=$(mktemp -d)
cp "$WHEEL" "$SRV_CTX/"
cp "$ACME_DIR/scripts/server-ext.Dockerfile" "$SRV_CTX/Dockerfile"
docker build -q --build-arg BASE="$REG/knowwork-server:$BASE_TAG" \
  --build-arg EXT_VERSION="$EXT_TAG" -t "$REG/knowwork-server:$EXT_TAG" "$SRV_CTX/"
rm -rf "$SRV_CTX"

echo "== 3/4 gateway 注入前端镜像 =="
GW_CTX=$(mktemp -d)
rsync -a --exclude node_modules --exclude dist --exclude '.env*' "$KW_DIR/web/" "$GW_CTX/kw-web/"
rsync -a "$ACME_DIR/frontend/" "$GW_CTX/kw-web/ext-frontend/"
mkdir -p "$GW_CTX/deploy/gateway"
cp "$KW_DIR/deploy/gateway/nginx.conf" "$GW_CTX/deploy/gateway/"
cp "$ACME_DIR/scripts/gateway-ext.Dockerfile" "$GW_CTX/Dockerfile"
docker build -q -t "$REG/knowwork-gateway:$EXT_TAG" "$GW_CTX/"
rm -rf "$GW_CTX"

echo "== 4/4 runtime-host 重打标签 =="
docker tag "$REG/knowwork-runtime-host:$BASE_TAG" "$REG/knowwork-runtime-host:$EXT_TAG"

echo "✓ 三个镜像就绪（$EXT_TAG）。切换：deploy/.env 的 KNOWWORK_VERSION=$EXT_TAG → docker compose up -d"
