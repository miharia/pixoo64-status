FROM python:3.12-slim

LABEL org.opencontainers.image.source="https://github.com/miharia/pixoo64-status"

WORKDIR /app

# 中文字体随项目分发（OFL 开源协议），构建过程无需联网
COPY fonts/ fonts/

COPY status_server.py ./

RUN pip install --no-cache-dir "Pillow>=10.0"

# 数据目录：状态文件 + 网页预览图，部署时通过 volume 挂载
RUN mkdir -p /app/data

ENV STATE_FILE=/app/data/status.json \
    PREVIEW_FILE=/app/data/status_preview.png \
    STATUS_PORT=8000

EXPOSE 8000

CMD ["python", "status_server.py"]
