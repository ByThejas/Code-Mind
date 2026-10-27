# Test image for the "node-test" profile (node --test). Build once:
#   docker build -t codemind-sandbox-node:1 -f sandbox/node.Dockerfile sandbox
# Runs have no network, so dependencies a repository's tests need must be installed here.
FROM node:20-slim
USER 65534:65534
WORKDIR /work
