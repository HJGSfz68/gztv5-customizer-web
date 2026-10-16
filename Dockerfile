FROM ubuntu:24.04
ENV DEBIAN_FRONTEND=noninteractive
RUN apt-get update && apt-get install -y --no-install-recommends \
      apktool aapt apksigner zipalign unzip libsmali-java openjdk-17-jre-headless \
      python3 python3-venv ca-certificates \
    && rm -rf /var/lib/apt/lists/*
RUN python3 -m venv /opt/venv
ENV PATH="/opt/venv/bin:${PATH}"
WORKDIR /app
COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt
COPY webapp.py customize_gztv5.py index.html ./
RUN chmod 0644 webapp.py customize_gztv5.py index.html \
    && groupadd --gid 10001 webapp && useradd --uid 10001 --gid 10001 --no-create-home --shell /usr/sbin/nologin webapp \
    && mkdir -p /home/webapp /srv/runtime/jobs \
    && chown -R 10001:10001 /home/webapp /srv/runtime
ENV GZTV5_BASE_APK=/srv/private/base.apk \
    GZTV5_KEYSTORE=/srv/private/signing.p12 \
    GZTV5_PASSFILE=/srv/private/signing.pass \
    GZTV5_RUNTIME_DIR=/srv/runtime
ENV HOME=/tmp
EXPOSE 8080
USER 10001:10001
CMD ["nice", "-n", "10", "uvicorn", "webapp:app", "--host", "0.0.0.0", "--port", "8080", "--workers", "1"]
