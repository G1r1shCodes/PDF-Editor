# Stage 1: Build the React frontend
FROM node:18 AS frontend-builder
WORKDIR /app
COPY package*.json ./
RUN npm install
COPY . .
RUN npm run build

# Stage 2: Setup the Python backend
FROM python:3.10-slim AS backend
WORKDIR /app

# Install system dependencies needed for Playwright, MinerU, and PyMuPDF
RUN apt-get update && apt-get install -y 
    build-essential 
    libgl1 
    libglib2.0-0 
    wget 
    curl 
    && rm -rf /var/lib/apt/lists/*

# Copy backend dependencies
COPY requirements.txt .

# Install Python dependencies
RUN pip install --no-cache-dir --upgrade pip && 
    pip install --no-cache-dir -r requirements.txt && 
    pip install --no-cache-dir -U "mineru[core]"

# Install Playwright browser and its system dependencies
RUN playwright install --with-deps chromium

# Copy the rest of the backend files
COPY . .

# Copy the built frontend from the previous stage
COPY --from=frontend-builder /app/dist /app/dist

# Expose the port Hugging Face Spaces expects
EXPOSE 7860

# Set Hugging Face cache directory (optional, prevents permission issues)
ENV TRANSFORMERS_CACHE=/app/.cache
ENV XDG_CACHE_HOME=/app/.cache
RUN mkdir -p /app/.cache && chmod 777 /app/.cache

# Start the FastAPI app on 0.0.0.0 and port 7860
CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "7860"]
