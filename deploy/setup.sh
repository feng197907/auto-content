#!/usr/bin/env bash
# 服务器部署脚本 —— 请在你自己的云服务器上手动执行。
# 按咱们的约定，服务器操作由你本人完成，这里给全命令清单。
#
# 用法（在服务器上）：
#   1. 把 auto-content/ 整个目录上传到服务器 /opt/auto-content
#   2. cd /opt/auto-content && bash deploy/setup.sh

set -euo pipefail

APP_DIR="/opt/auto-content"
SERVICE="auto-content"

echo "==> 1. 检测包管理器"
if command -v dnf >/dev/null 2>&1; then
    PKG="dnf"
elif command -v yum >/dev/null 2>&1; then
    PKG="yum"
elif command -v apt-get >/dev/null 2>&1; then
    PKG="apt-get"
else
    echo "未识别的包管理器，请手动安装 python3 nginx" >&2
    exit 1
fi

echo "==> 2. 安装依赖"
if [ "$PKG" = "apt-get" ]; then
    sudo apt-get update -y
    sudo apt-get install -y python3 python3-pip python3-venv nginx
else
    sudo "$PKG" install -y python3 python3-pip nginx
fi

echo "==> 3. 创建虚拟环境并安装 Python 依赖"
cd "$APP_DIR"
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt

echo "==> 4. 提醒填写配置（.env 文件）"
echo "请确认 .env 里已配置以下三项："
echo "  ZHIPU_API_KEY=你的智谱key（免费）"
echo "  SERVERCHAN_KEY=你的Server酱key（微信推送）"
echo "  REVIEW_URL=https://你的域名/?pwd=你的密码（手机审核链接）"
echo "  APP_PASSWORD=你的审核密码（防止别人乱点）"
read -r -p "已填好 .env 吗？(y/n) " answer
if [ "$answer" != "y" ]; then
    echo "请先填写 .env，再重新运行本脚本。"
    exit 0
fi

echo "==> 5. 安装 systemd 服务（审核后台常驻）"
sudo cp deploy/auto-content.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable "$SERVICE"
sudo systemctl start "$SERVICE"

echo "==> 6. 安装定时任务（每天 8:00 生成稿件并推送微信）"
(crontab -l 2>/dev/null | grep -v "daily_job.py"; echo "0 8 * * * cd $APP_DIR && $APP_DIR/.venv/bin/python daily_job.py >> $APP_DIR/data/cron.log 2>&1") | crontab -

echo "==> 7. 配置 Nginx 反代"
sudo cp deploy/nginx.conf /etc/nginx/conf.d/auto-content.conf
sudo nginx -t
sudo systemctl reload nginx

echo "==> 完成！"
echo "审核后台：http://你的服务器IP（或配域名后 HTTPS 访问）"
echo ""
echo "常用命令："
echo "  查看服务状态: sudo systemctl status $SERVICE"
echo "  手动生成稿件: cd $APP_DIR && .venv/bin/python pipeline.py"
echo "  手动生成+推送: cd $APP_DIR && .venv/bin/python daily_job.py"
echo "  手动推送微信: cd $APP_DIR && .venv/bin/python notify.py"
echo "  查看生成日志: tail -f $APP_DIR/data/cron.log"
