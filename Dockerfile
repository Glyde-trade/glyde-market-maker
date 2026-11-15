FROM python:3.11-slim
WORKDIR /app
COPY pyproject.toml README.md LICENSE NOTICE ./
COPY src/ ./src/
RUN python -m pip install --no-cache-dir . \
    && useradd --uid 10001 --create-home maker \
    && mkdir -p /work/.glyde-mm && chown -R maker:maker /work
USER maker
WORKDIR /work
ENTRYPOINT ["glyde-mm"]
CMD ["--help"]
