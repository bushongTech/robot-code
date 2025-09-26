FROM python:latest

# Tk dependencies for the optional UI
RUN apt-get update \
  && apt-get install -y --no-install-recommends python3-tk tk \
  && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Copy requirements first to leverage Docker cache
COPY robot-code/requirements.txt /app/requirements.txt

# Install Python dependencies
RUN pip install --no-cache-dir -r requirements.txt

# Copy app code
COPY robot-code /app

# Ensure config path exists (compose will mount the real file)
RUN mkdir -p /app/config

CMD ["python", "-u", "main.py"]
