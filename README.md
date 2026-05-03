# GeminiGen

## 服务器更新与部署（推荐）

在服务器上执行：

```bash
cd /opt/GeminiGen
bash update.sh
```

指定分支部署：

```bash
cd /opt/GeminiGen
bash update.sh main
# 或
bash update.sh claude/fix-git-pull-connection-1E8wW
```

> `update.sh` 会自动：
> - `git fetch --all --prune`
> - 自动暂存本地修改（含未跟踪文件）
> - 分支不存在时回退到 `origin/main`
> - 执行 `deploy.sh`，重建 `.venv` 并重写 systemd 配置

## 故障快速修复

当服务报 `status=203/EXEC`（通常是 `.venv` 被删）时：

```bash
cd /opt/GeminiGen
bash deploy.sh
systemctl status geminigen --no-pager
```


## 当 `update.sh` 不存在时

```bash
cd /opt/GeminiGen
bash deploy.sh
systemctl daemon-reload
systemctl restart geminigen
systemctl status geminigen --no-pager
```


## 修复 `git pull` 提示 no such ref

当出现：
`Your configuration specifies to merge with ... no such ref was fetched`

执行：

```bash
cd /opt/GeminiGen
bash recovery.sh
```

这会自动修复 upstream 到 `origin/main`，强制同步代码并重新部署。
