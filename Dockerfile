FROM python:3.12-slim
WORKDIR /app
COPY pyproject.toml .
COPY ssrfobs ssrfobs
RUN pip install --no-cache-dir .
VOLUME /data
CMD ["ssrf-obs", "run"]
