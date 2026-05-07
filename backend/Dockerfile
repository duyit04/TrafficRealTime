FROM python:3.11-slim

WORKDIR /app

# Install system dependencies for OpenCV and other packages
RUN apt-get update && apt-get install -y --no-install-recommends \
    libgl1 \
    libglib2.0-0 \
    libsm6 \
    libxext6 \
    libxrender-dev \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
ENV PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DEFAULT_TIMEOUT=600 \
    PIP_RETRIES=10
# Default pip PyTorch is CPU-only; overlay a CUDA build so NVIDIA Container Toolkit can use the GPU.
RUN pip install -r requirements.txt --timeout 600 --retries 10 \
    && pip install --no-cache-dir --force-reinstall torch torchvision \
        --index-url https://download.pytorch.org/whl/cu124

COPY . .

RUN mkdir -p models_storage uploads

EXPOSE 8000

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
