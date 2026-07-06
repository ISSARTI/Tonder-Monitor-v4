# Use a standard Python slim image
FROM python:3.11-slim

# Set working directory
WORKDIR /app

# Install tini to manage zombie processes in Docker container
RUN apt-get update && apt-get install -y tini && rm -rf /var/lib/apt/lists/*

# Copy pyproject.toml to install dependencies
COPY pyproject.toml ./

# Install python dependencies (including playwright python package)
RUN pip install --no-cache-dir .

# Install websockets dependency directly (keeping pyproject.toml untouched)
RUN pip install --no-cache-dir websockets>=12.0

# Install Chromium browser and its OS dependencies
RUN playwright install chromium
RUN playwright install-deps chromium

# Copy the scripts and mock portal files
COPY toender_watch_v4.py run_uat_test_v4.py mock_danish_portal.html dummy_autofill_test.html ./

# Use tini as the init entrypoint
ENTRYPOINT ["/usr/bin/tini", "--"]

# Run in unbuffered mode so logs show up instantly
CMD ["python", "-u", "toender_watch_v4.py"]
