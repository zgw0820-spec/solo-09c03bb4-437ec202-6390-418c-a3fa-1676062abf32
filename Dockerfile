FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    DATA_DIR=/data \
    IMAGE_SIZE_MIB=8 \
    LEASE_TTL_SECONDS=60

WORKDIR /srv
COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt
COPY app ./app

# 数据卷：镜像、暂存、journal、演示备份段都在 /data 下，修复成果可持久化
VOLUME ["/data"]
EXPOSE 8000

# 单 worker：租约表在进程内存中（见 README 的已知限制）
CMD ["python", "-m", "uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
