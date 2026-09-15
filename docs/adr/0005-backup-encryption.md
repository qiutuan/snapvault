# ADR-0005: 备份与加密方案 — SQLite 在线快照 + AES-256-GCM 分块加密

- 状态：已采纳

## 背景
M9 要求：定时自动备份（保留 7 份）、可选本地 AES-256 加密（口令派生）、
整库导出/导入迁移、隐私自检证明零网络。

## 决策
1. **快照**：用 SQLite 在线备份 API（`src_conn.backup(dst)`）跨连接复制一致快照，
   对 WAL 模式安全（不会出现半页写入）；快照目录含 `snapvault.db + manifest.json`
   （资产清单+哈希），保留最近 7 份（可配）；
2. **加密**：文件格式为分块 AEAD —— header(`SNPVLT1` + version + salt(16) + iv(12) + chunk_size(4))，
   每块独立 GCM 认证（AAD=块序号，防重排），口令经 PBKDF2-HMAC-SHA256（60 万次迭代）派生 AES-256 密钥；
   内存数据用单发 AEAD（header + tag + body）；任何块校验失败 → `CryptoError`（口令错误/篡改/截断统一处理）；
3. **整库迁移**：`export_library` 打包 快照DB + manifest + config + 原图 为 zip（带 `.snapvault_marker`），
   `import_library` 解包到新根目录并做完整性校验；
4. **隐私自检**：psutil 采集本进程 TCP/UDP 连接快照，执行一轮本地活动后对比"无新增出站连接"，
   叠加核心模块静态 import 扫描（禁止 requests/urllib/socket 等）。

## 理由
- 不对活库原地加密：WAL/vec0 实时读写要求随机访问，原地加密会破坏原子性与迁移能力；
  加密作用于快照与迁移包，兼顾安全与一致性。
- GCM 分块避免整库载入内存，支持任意大库。

## 后果
- 加密快照恢复需要口令；丢失口令则无法解密（设计如此，README 已提示）。
- 备份目录保留策略默认 7 份，可配置 `backup_keep`。
