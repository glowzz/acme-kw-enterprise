# 自研扩展叠装进开源 server 镜像（设计文档 9.2 方案 A）。
# 构建上下文 = 含扩展 wheel 的目录；授权随 wheel 里的 license Verifier 自动生效（事实 6）。
# docker build -f scripts/server-ext.Dockerfile --build-arg BASE=<开源server镜像> \
#   --build-arg EXT_VERSION=<扩展标签> -t <registry>/knowwork-server:<扩展标签> <含wheel的目录>
ARG BASE
FROM ghcr.io/astral-sh/uv:0.7.8 AS uv
FROM ${BASE}
USER root
COPY --from=uv /uv /usr/local/bin/uv
COPY acme_knowwork_enterprise-*.whl /tmp/
RUN uv pip install --python /opt/venv/bin/python /tmp/acme_knowwork_enterprise-*.whl && rm /tmp/*.whl
# 系统设置页显示的版本号换成扩展标签
ARG EXT_VERSION
ENV KNOWWORK_VERSION=${EXT_VERSION}
USER 10001
