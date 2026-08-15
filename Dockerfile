# Use an official lightweight Python image
FROM python:3.10-slim

# Set the working directory inside the container
WORKDIR /app

RUN apt-get update && apt-get install -y --no-install-recommends \
    libgdal-dev gdal-bin && rm -rf /var/lib/apt/lists/*

# Copy the requirements file and install dependencies
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy the rest of your application code
COPY . .

# Expose the port Hugging Face expects (7860)
EXPOSE 7860

# Run the application (adjusting for Hugging Face's required port)
#
# NOTE on --workers/--threads: this app's in-memory caches (translation
# cache, crop-AI cache, Agmarknet fetch cache) are plain Python dicts.
# With --workers 2, gunicorn spawns 2 separate OS processes, each with its
# OWN copy of those dicts — a request handled by worker B can't see what
# worker A already cached, which silently halves the effective cache hit
# rate under real traffic and doubles hits to slow external APIs.
# Since this app is I/O-bound (waiting on OpenWeather/Groq/Agmarknet, not
# doing heavy CPU work), a single worker with more threads shares memory
# and gets you real concurrency without duplicating caches.
CMD ["gunicorn", "--bind", "0.0.0.0:7860", "--workers", "1", "--threads", "8", "--timeout", "60", "app:app"]
