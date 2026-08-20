# 部署

```bash
mkdir -p ~/.config/systemd/user
cp deploy/*.service ~/.config/systemd/user/
systemctl --user daemon-reload
systemctl --user enable --now techradar-bot techradar-scheduler techradar-web
loginctl enable-linger $USER      # 让 user service 在未登录时也运行
journalctl --user -u techradar-scheduler -f
```
临时运行：`nohup .venv/bin/techradar bot > logs/bot.log 2>&1 &`，`nohup .venv/bin/techradar run > logs/scheduler.log 2>&1 &`
