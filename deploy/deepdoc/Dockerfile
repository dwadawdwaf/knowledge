# 复用上游源码、依赖与随镜像分发的模型；不启动上游平台服务。
ARG RAGFLOW_IMAGE=infiniflow/ragflow:v0.22.0
FROM ${RAGFLOW_IMAGE}
WORKDIR /ragflow
COPY deploy/deepdoc/requirements.lock /opt/ks-sidecar-requirements.lock
# 边车 HTTP 依赖隔离安装，不改写上游环境；共享上游重型解析库。
RUN uv venv --python /ragflow/.venv/bin/python /opt/ks-sidecar-venv \
    && uv pip install --python /opt/ks-sidecar-venv/bin/python --no-deps -r /opt/ks-sidecar-requirements.lock
# 新 venv 不继承父 venv 的包；追加上游包路径，边车自有依赖仍优先。
RUN upstream_site=$(/ragflow/.venv/bin/python -c 'import sysconfig; print(sysconfig.get_path("purelib"))') \
    && sidecar_site=$(/opt/ks-sidecar-venv/bin/python -c 'import sysconfig; print(sysconfig.get_path("purelib"))') \
    && printf '%s\n' "$upstream_site" > "$sidecar_site/ragflow-upstream.pth" \
    && /opt/ks-sidecar-venv/bin/python -c "import PIL, pypdf, fastapi; assert fastapi.__version__ == '0.115.12'"
COPY deploy/deepdoc/service_conf.yaml /ragflow/conf/service_conf.yaml
COPY deploy/deepdoc/LICENSE.ragflow /opt/ks/LICENSE.ragflow
RUN /ragflow/.venv/bin/python -c "from pathlib import Path; assert Path('/ragflow/VERSION').read_text().strip() == 'v0.22.0', 'Unexpected upstream version'"
COPY backend/knowledge /opt/ks/knowledge
COPY backend/deepdoc_sidecar /opt/ks/deepdoc_sidecar
COPY deploy/deepdoc/gateway.conf /opt/ks/gateway.conf
RUN ln -sf /dev/stderr /var/log/nginx/error.log
ENV PYTHONPATH=/opt/ks:/ragflow HF_HUB_OFFLINE=1 TRANSFORMERS_OFFLINE=1 DEVICE=cpu
ENTRYPOINT ["/opt/ks-sidecar-venv/bin/python", "-m", "uvicorn", "deepdoc_sidecar.service:create_app", "--factory", "--host", "0.0.0.0", "--port", "9381", "--no-access-log"]
