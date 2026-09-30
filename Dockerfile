# Use a slim Python base image
FROM python:3.11-slim

# Install system dependencies needed for FFmpeg, macvlan creation, DHCP, and PyTorch (libgomp1)
RUN apt-get update && apt-get install -y \
    ffmpeg \
    iproute2 \
    isc-dhcp-client \
    procps \
    sudo \
    curl \
    libgomp1 \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Detect host architecture at build time and download the correct MediaMTX v1.18.2.
# This places it directly in the working directory so MediaMTXManager skips downloading it at runtime.
RUN ARCH=$(dpkg --print-architecture) && \
    if [ "$ARCH" = "amd64" ]; then \
        MEDIAMTX_ARCH="amd64"; \
    elif [ "$ARCH" = "arm64" ]; then \
        MEDIAMTX_ARCH="arm64"; \
    elif [ "$ARCH" = "armhf" ]; then \
        MEDIAMTX_ARCH="armv7"; \
    else \
        MEDIAMTX_ARCH="amd64"; \
    fi && \
    curl -L -o mediamtx.tar.gz "https://github.com/bluenviron/mediamtx/releases/download/v1.18.2/mediamtx_v1.18.2_linux_${MEDIAMTX_ARCH}.tar.gz" && \
    tar -xzf mediamtx.tar.gz mediamtx && \
    rm mediamtx.tar.gz && \
    chmod +x mediamtx

# Install core Python dependencies
RUN pip install --no-cache-dir \
    flask \
    flask-cors \
    requests \
    pyyaml \
    psutil \
    onvif-zeep \
    apprise \
    paramiko \
    cryptography \
    paho-mqtt

# Install CPU-only PyTorch first (keeps the image small, ~200MB vs ~2GB for GPU)
RUN pip install --no-cache-dir \
    torch \
    torchvision \
    --index-url https://download.pytorch.org/whl/cpu

# Install AI dependencies (ultralytics / YOLO + headless OpenCV)
RUN pip install --no-cache-dir \
    ultralytics \
    opencv-python-headless \
    easyocr \
    huggingface-hub

# Pre-download the default YOLO model so it's available immediately at runtime
RUN python -c "from ultralytics import YOLO; YOLO('yolov8n.pt')"

# Bundle the pinned plate detector and English OCR weights, so these inherited
# Tony features work with internet access disabled on the camera VLAN.
COPY app/ai_device.py app/config.py /app/app/
RUN python -c "from app.ai_device import get_shared_plate_model, get_shared_ocr_reader; from pathlib import Path; import shutil; model = get_shared_plate_model(); Path('models').mkdir(exist_ok=True); shutil.copyfile(model.ckpt_path, 'models/license_plate.pt'); get_shared_ocr_reader()"

# Keep model assets cached when application/UI code changes.
COPY . .

# Default Web UI port. Override at build/run time with the WEB_UI_PORT env var.
# (With network_mode: host this is informational; the app binds this port on the host.)
ENV WEB_UI_PORT=5552
EXPOSE ${WEB_UI_PORT}

# Run the app
CMD ["python", "run.py"]
