ARG ROOFER_IMAGE=3dgi/roofer:v1.0.0@sha256:dd2c415aaee337502bde0dc1426dfa9c9f88e648f9d2f6340110c49932c251d2
FROM ${ROOFER_IMAGE} AS roofer

FROM mambaorg/micromamba:2.3.3@sha256:800e7ade3ffe29c9a9ac2026163131495f8197c3852e572c5835beb4e8a33cd6

COPY --chown=$MAMBA_USER:$MAMBA_USER environment.yml /tmp/environment.yml
RUN micromamba install --yes --name base --file /tmp/environment.yml \
    && micromamba clean --all --yes

USER root
COPY --from=roofer /opt/roofer/ /opt/roofer/
RUN mkdir -p /srv/cape-roof-geometry /tmp/cape-roof-geometry \
    && chown -R $MAMBA_USER:$MAMBA_USER /srv/cape-roof-geometry /tmp/cape-roof-geometry

USER $MAMBA_USER
WORKDIR /srv/cape-roof-geometry
COPY --chown=$MAMBA_USER:$MAMBA_USER app ./app
COPY --chown=$MAMBA_USER:$MAMBA_USER config ./config
COPY --chown=$MAMBA_USER:$MAMBA_USER LICENSE ./LICENSE

ENV PATH=/opt/roofer/bin:$PATH \
    GDAL_DATA=/opt/conda/share/gdal \
    PROJ_DATA=/opt/conda/share/proj \
    PROJ_LIB=/opt/conda/share/proj \
    ROOFER_GDAL_DATA=/opt/roofer/share/gdal \
    ROOFER_PROJ_DATA=/opt/roofer/share/proj \
    PYTHONUNBUFFERED=1 \
    WORK_ROOT=/tmp/cape-roof-geometry

EXPOSE 8080
HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
  CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8080/healthz', timeout=3)"

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8080", "--workers", "1", "--proxy-headers"]
