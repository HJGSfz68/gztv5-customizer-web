# GZTV5 APK 网页定制台

自托管中文 Web 应用，可修改应用名称、Android 包名、主网址和桌面图标，并基于固定基准 APK 构建签名后的 APK。

## 默认行为与安全提醒

一键安装脚本默认启用**公开免登录模式**，监听 `0.0.0.0:1212`。任何能访问服务器的人都可以提交构建、下载服务器签名的 APK；公开模式一次只接受一个活跃任务。脚本会限制服务为 75% CPU、448 MiB 内存和 288 MiB Java 堆，以降低对同机其他服务的影响，但这些限制不能阻止滥用。不要把敏感网址、图标或其他私密信息提交到公开页面；如不需要公开访问，应使用 HTTPS 反向代理、访问控制或将服务改为仅本机监听。

本项目源码包**包含**构建所需的 `private/base.apk`，但**不包含**签名证书、证书口令、`.env`、生成 APK、运行目录、虚拟环境、缓存或日志。公开分享基准 APK 前，请先确认你有权再分发它。构建必须另行提供 `private/signing.p12` 与 `private/signing.pass`；若要覆盖升级已安装版本，签名证书必须与基准 APK 的证书一致。请勿把密钥或口令提交到公开仓库。

## 一键部署（推荐）

适用于 Ubuntu 24.04+、systemd 主机，需要 root/sudo 和网络连接。脚本会安装 APK 工具链与 Python 依赖、创建 `webapp` 系统用户、生成随机会话密钥、写入受限的 systemd 服务并运行健康检查；不需要常驻 Docker 守护进程。

1. 将 ZIP 解压到 `/opt`，使项目目录为 `/opt/GZTV5_customizer_web`：

   ```bash
   sudo unzip GZTV5_customizer_web-complete-source.zip -d /opt
   cd /opt/GZTV5_customizer_web
   ```

2. 基准 APK 已在 `private/base.apk`。通过安全渠道另行放入匹配的签名证书和口令文件：

   ```text
   private/signing.p12
   private/signing.pass
   ```

   `signing.pass` 文件内容应为证书口令。脚本会检查这两个文件是否存在，并收紧文件权限；不会显示或上传口令。

3. 一键安装并启动（默认端口就是 1212）：

   ```bash
   sudo bash deploy/install.sh
   # 可选：使用其他端口
   sudo bash deploy/install.sh --port 1212
   ```

   首次安装会在 `/etc/gztv5-customizer.env` 创建随机 `APP_SECRET_KEY`，权限设为 `0600`，不会打印密钥。若 UFW 已启用，脚本会放行所选 TCP 端口；如果服务器位于云上，还需在云厂商安全组/网络 ACL 中允许入站 TCP 1212。已有环境文件会被保留，脚本不会覆盖密钥；若其路径与当前项目目录不匹配，脚本会停止并提示人工核对。

4. 访问：

   ```text
   http://<服务器公网 IP>:1212/
   ```

   本机健康检查：

   ```bash
   curl -fsS http://127.0.0.1:1212/healthz
   systemctl status gztv5-customizer.service --no-pager
   journalctl -u gztv5-customizer.service -f
   ```

脚本默认在公网网卡提供 HTTP 服务。正式使用建议放在 HTTPS 反向代理之后，并按需要配置访问控制、速率限制和云防火墙。若要让服务改为私有管理员模式，需设置 `PUBLIC_MODE=0` 和至少 16 位的 `ADMIN_PASSWORD`，并将 systemd unit 的监听地址改为 `127.0.0.1`；通过 HTTPS 反代时再设置 `COOKIE_SECURE=1`，然后重启服务。

## Docker Compose 部署（可选）

Compose 配置也默认公开监听 1212，并使用 CPU/内存/Java 堆限额。需要 Docker Engine 与 Compose v2。签名材料仍须先放在 `private/`。

```bash
umask 077
app_secret_key="$(openssl rand -hex 32)"
printf 'PUBLIC_MODE=1\nBIND_ADDRESS=0.0.0.0\nHOST_PORT=1212\nCOOKIE_SECURE=0\nAPP_SECRET_KEY=%s\nBUILD_CPU_LIMIT=0.75\nBUILD_MEMORY_LIMIT=448m\nJAVA_MAX_HEAP=288m\n' \\
  "$app_secret_key" > .env
unset app_secret_key
chmod 600 .env
docker compose up -d --build
curl -fsS http://127.0.0.1:1212/healthz
```

## 服务管理与排错

```bash
sudo systemctl restart gztv5-customizer.service
sudo systemctl status gztv5-customizer.service --no-pager
sudo journalctl -u gztv5-customizer.service -n 100 --no-pager
```

如果服务未启动，先检查 `private/signing.p12`、`private/signing.pass` 是否存在且证书与基准 APK 匹配。内存上限有意设置得较低；大型 APK 构建可能更慢或因 Java 堆上限而失败。构建状态保存在内存中，服务重启后未完成的任务需重新提交。主网址写入客户端 APK，服务器不会主动访问该网址。

## 许可证与素材

本项目原创源码采用 MIT 许可证，详见 [`LICENSE`](LICENSE)。该许可证**不适用于** `private/base.apk`、签名证书/口令或第三方素材；这些材料的权利仍归各自权利人所有。基准 APK 随本仓库提供仅供有权使用者按适用条款使用，不因本项目源码采用 MIT 许可证而获得额外授权。签名证书和口令不会随公开仓库发布。
