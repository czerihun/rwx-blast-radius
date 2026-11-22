# The scorer builds this on linux/amd64 and runs it with no network. The
# digest is the image we labeled every commit in: its Python and SQLite
# decide which sqlglot tests run, so keep it if your image runs them.
FROM python:3.13-slim@sha256:e48f59cdba69a3cbd9ed2cf0f348d90d45a8d350d98134cf1070bc106ee9ed90
COPY selector.py /app/selector.py
COPY heuristics.py /app/heuristics.py
COPY data/index.bin /app/data/index.bin
COPY data/mapping.bin /app/data/mapping.bin
COPY data/identity.bin /app/data/identity.bin
ENV PYTHONDONTWRITEBYTECODE=1
ENV BR_IDENTITY_MODE=observed BR_ORDER_MODE=observed BR_MAPPING_MODE=optimistic \
    BR_PRUNE_FRESH_COPY=1 BR_PRUNE_IMPORTS=1
CMD ["python", "/app/selector.py"]
