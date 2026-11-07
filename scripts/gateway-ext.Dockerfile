# 带扩展前端的网关镜像：vite 构建时经 KNOWWORK_WEB_EXTENSIONS 注入扩展入口（vite.config.ts
# 的 @knowwork/extensions 别名）。构建上下文由 scripts/build_ext_images.sh 拼装：
#   kw-web/                开源 web 源码（排除 node_modules、.env*、dist）
#   kw-web/ext-frontend/   扩展前端（放进 web 树内，npm 依赖沿目录向上解析到 kw-web/node_modules）
#   deploy/gateway/nginx.conf
ARG NODE_IMAGE=node:24.18.0-bookworm-slim
ARG NGINX_IMAGE=nginxinc/nginx-unprivileged:1.28-alpine
FROM --platform=$BUILDPLATFORM ${NODE_IMAGE} AS web
WORKDIR /web
COPY kw-web/package.json kw-web/package-lock.json ./
RUN --mount=type=cache,target=/root/.npm \
    npm ci --no-audit --no-fund
COPY kw-web/ ./
ENV KNOWWORK_WEB_EXTENSIONS=/web/ext-frontend/src/install.tsx
RUN npm run build
FROM ${NGINX_IMAGE} AS runtime
COPY deploy/gateway/nginx.conf /etc/nginx/conf.d/default.conf
COPY --from=web /web/dist /usr/share/nginx/html
EXPOSE 8080
