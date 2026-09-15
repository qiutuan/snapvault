# SnapVault CLI 使用文档

统一入口：`snapvault`（`python3 -m snapvault.cli`）。数据根目录：
`--data-dir` 参数 或 环境变量 `SNAPVAULT_DATA_DIR` 或 默认 `~/SnapVault`。

## 命令一览

| 命令 | 说明 |
|---|---|
| `init` | 初始化数据根目录 + 启动网络探测自检 |
| `import PATH... [--dir] [--source S] [--process]` | 导入文件/目录（增量去重） |
| `process [--max-runs N]` | 处理异步队列（OCR/embedding/缩略图） |
| `search Q [--k N] [--tag T] [--since D] [--until D] [--min-conf F] [--size WxH] [--json]` | 混合搜索 |
| `similar ID [--k N]` | 以图搜图 |
| `stats [--json]` | 资产/任务/存储统计 |
| `tag list/set ID TAGS.../rm TAG [--rename-to X]` | 标签管理 |
| `note get/set/rm ID` | 备注（Markdown） |
| `field list/define KEY LABEL [--type T]/set ID KEY VALUE/get ID` | 自定义字段 |
| `annotate get ID [--out F] / save ID F / raster ID --out F` | 标注图层 |
| `export [--include annotated\|original] [--pdf] [--no-csv] [--tag T] [--out D]` | 批量导出 |
| `rebuild [--force] [--no-ocr] [--no-embed] [--no-thumbs]` | 离线重建索引 |
| `backup [--passphrase P]` / `backups` / `restore SNAP [--passphrase P]` | 快照 |
| `export-library [--out F]` / `import-library ZIP [--target D]` | 整库迁移 |
| `privacy-check [--json]` | 隐私自检报告 |
| `trash move/restore/purge/list ID` | 回收站 |
| `verify-integrity` | 数据库完整性校验 |
| `version` | 版本 |

## 典型工作流

```bash
# 初始化
snapvault init

# 导入目录并立即处理 OCR/向量
snapvault import ~/Pictures/截图 --dir --process

# 搜索画面中的文字
snapvault search "支付成功" --k 10

# 打标签 + 写备注 + 自定义字段
snapvault tag set 3 项目A/登录页 缺陷
snapvault note set 3 "第 3 次登录失败"
snapvault field define defect_id 缺陷ID --type text
snapvault field set 3 defect_id BUG-1024

# 保存/查看标注（图层 JSON）
snapvault annotate get 3 --out anno.json
snapvault annotate save 3 anno.json

# 导出（标注打码版 + CSV + PDF）
snapvault export --include annotated --pdf --out ~/导出包

# 加密备份 / 恢复
snapvault backup --passphrase 我的口令
snapvault restore backups/snapshot_xxx --passphrase 我的口令

# 整库迁移
snapvault export-library --out migration.zip
snapvault import-library migration.zip --target ~/新数据根

# 隐私自检
snapvault privacy-check
```

## 退出码
0 成功；1 执行错误；130 用户中断。
