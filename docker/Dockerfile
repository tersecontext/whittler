FROM node:20-slim

# Install Claude Code CLI
RUN npm install -g @anthropic-ai/claude-code

# Install common tools
RUN apt-get update && apt-get install -y \
    git \
    python3 \
    python3-pip \
    build-essential \
    jq \
    && rm -rf /var/lib/apt/lists/*

# Configure git (needed for Claude Code to commit)
RUN git config --global user.email "whittler@agent" && \
    git config --global user.name "Whittler Agent"

WORKDIR /work

COPY entrypoint.sh /entrypoint.sh
RUN chmod +x /entrypoint.sh

ENTRYPOINT ["/entrypoint.sh"]
