# FlyTrack Cloud 云端版

这是 FlyTrack 的云端部署版本。服务端使用 Python 标准库、HTTP Cookie 会话和 SQLite，浏览器端复用主程序界面。

## 功能

- 所有用户通过账号、密码登录
- 第一位管理员由服务器环境变量创建
- 管理员可在页面上添加用户、设置初始密码、调整角色、停用账号和重置密码
- 所有账号共享同一套云端果蝇数据
- 数据保存在服务器的 SQLite 数据库中
- 支持多台电脑、手机同时访问并自动同步
- 无第三方 Python 依赖

## 目录结构

- `server.py`：云端服务、账号、会话和 SQLite API
- `public/`：浏览器端程序
- `Dockerfile`：Docker 镜像
- `docker-compose.yml`：VPS 部署
- `render.yaml`：Render Blueprint 示例
- `Procfile`：支持 Procfile 的平台

## 部署前必须设置的环境变量

| 变量 | 示例 | 说明 |
|---|---|---|
| `ADMIN_USERNAME` | `admin` | 初始管理员用户名 |
| `ADMIN_PASSWORD` | 一串长随机密码 | 首次启动时创建管理员；至少 8 位 |
| `ADMIN_DISPLAY_NAME` | `实验室管理员` | 页面上显示的管理员名称 |
| `DATA_DIR` | `/data` | SQLite 数据库所在持久化目录 |
| `COOKIE_SECURE` | `1` | HTTPS 云端必须设为 `1`；本地 HTTP 测试设为 `0` |
| `PORT` | `8000` | 服务监听端口，多数平台会自动提供 |
| `SESSION_DAYS` | `30` | 登录保持天数 |
| `RESET_ADMIN_PASSWORD` | `0` | 改为 `1` 并重新部署可重置管理员密码 |

不要将真实 `ADMIN_PASSWORD` 写入公开的 Git 仓库。

## 方案一：Render 部署

1. 在 GitHub 新建一个仓库。
2. 将本 `cloud` 文件夹中的所有内容上传为仓库根目录。
3. 登录 Render，选择 **New → Blueprint**，连接该仓库。
4. Render 会读取 `render.yaml`。创建服务时填写 `ADMIN_PASSWORD`。
5. 确认服务挂载持久化磁盘，路径为 `/var/data`。
6. 等待部署完成，打开 Render 提供的 HTTPS 地址。
7. 使用 `ADMIN_USERNAME` 和 `ADMIN_PASSWORD` 登录。
8. 点击页面右上角的“用户管理”添加其他账号。

`render.yaml` 使用了持久化磁盘。Render 的免费实例通常不支持持久磁盘，实际部署请选择支持磁盘的付费实例，否则重新部署可能丢失数据库。

## 方案二：Railway 部署

1. 将本 `cloud` 文件夹内容推送到 GitHub。
2. Railway 选择 **Deploy from GitHub Repo**。
3. 添加环境变量：
   - `ADMIN_USERNAME`
   - `ADMIN_PASSWORD`
   - `ADMIN_DISPLAY_NAME`
   - `DATA_DIR=/data`
   - `COOKIE_SECURE=1`
4. 在 Railway 中给服务挂载持久化 Volume，挂载路径设置为 `/data`。
5. 部署后打开公开 HTTPS 域名并登录。

必须挂载 Volume，否则每次重新部署都可能重置 SQLite 数据库。

## 方案三：普通 VPS / Docker Compose

服务器必须安装 Docker 与 Docker Compose。

```bash
cd cloud
cp .env.example .env
# 编辑 .env，至少修改 ADMIN_PASSWORD
docker compose up -d --build
```

浏览器访问：

```text
http://服务器IP:8000
```

正式使用建议使用 Caddy 或 Nginx 配置 HTTPS 域名，并将 `COOKIE_SECURE` 设置为 `1`。数据库会持久化在宿主机的 `cloud/data/flytrack.db`。

## 管理员操作

1. 使用初始管理员账号登录。
2. 点击页面右上角的“用户管理”。
3. 填写用户名、显示名称、初始密码和角色。
4. 新用户可直接用该账号登录。
5. 管理员可以停用用户、恢复启用、调整角色或重置密码。
6. 管理员不能停用或降级当前正在使用的管理员账号。

用户名允许 3–32 位英文、数字、点、横线和下划线；密码至少 8 位。

## 数据存储

- 云端数据在 `DATA_DIR/flytrack.db`
- 数据库使用 WAL 模式
- 所有账号共享一套实验室数据
- 建议只运行一个应用实例；SQLite 不适合多个云实例同时写入
- 定期在软件中导出 JSON 备份
- 必须挂载持久化磁盘或 Volume

## 本地测试

```powershell
$env:ADMIN_PASSWORD = "replace-with-a-long-password"
$env:COOKIE_SECURE = "0"
$env:DATA_DIR = "$PWD\data"
python server.py
```

打开 `http://localhost:8000`。本地 HTTP 必须使用 `COOKIE_SECURE=0`，否则浏览器不会发送登录 Cookie。

## 更新部署

更新代码后重新部署即可。只要 `DATA_DIR` 指向原持久化磁盘，账号和果蝇数据都会保留。
