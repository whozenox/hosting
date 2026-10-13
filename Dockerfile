FROM python:3.11-slim

# Made by @Dev_Null_X

WORKDIR /app

# System deps: build tools for Python libs, git for github.py, and
# Node.js/npm (used only by the panel itself). v5.2: hosted bots NEVER run
# on this host — Docker isolation is mandatory (see SECURITY.md).
RUN apt-get update && apt-get install -y --no-install-recommends \
    build-essential \
    git \
    curl \
    ca-certificates \
    ffmpeg \
    && curl -fsSL https://deb.nodesource.com/setup_20.x | bash - \
    && apt-get install -y --no-install-recommends nodejs \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

# Make sure the runtime folders exist even if git dropped empty dirs
RUN mkdir -p uploads containers backups logs temp database

CMD ["python", "bot.py"]
