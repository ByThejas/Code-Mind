# Test image for the "pytest" profile. Build once:
#   docker build -t codemind-sandbox-python:1 -f sandbox/python.Dockerfile sandbox
# Runs have no network, so anything a repository's tests import must be installed here.
ARG BASE=python:3.12-slim
FROM ${BASE}
RUN pip install --no-cache-dir "pytest>=8,<9"
USER 65534:65534
WORKDIR /work
