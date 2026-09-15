# SnapVault 架构文档

## 1. 总体架构

```
┌──────────────────────────── 桌面壳（Tauri 2, app/）────────────────────────────┐
│  托盘/全局快捷键/窗口    │  三栏 UI（文件夹树·缩略图瀑布流·详情）  │  引导/设置     │
└──────────────┬───────────────────────────────┬──────────────────────┘
               │ spawn sidecar（snapvault CLI）│ invoke
┌──────────────▼───────────────────────────────▼──────────────────────┐
│                核心逻辑层 core/snapvault（纯 Python）                │
│  engine.py（外观层） ⇄ cli.py（CLI 入口）/ serve.py（本地 HTTP）     │
│ ┌──────────┐ ┌──────────┐ ┌──────────┐ ┌──────────┐ ┌────────────┐  │
│ │ 采集导入  │ │ OCR 引擎 │ │ 向量引擎  │ │ 混合检索  │ │ 标注/导出   │  │
│ │ importer │ │ rapidocr │ │ fastembed │ │ search   │ │ annotations│  │
│ │ jobs     │ │  (离线包) │ │ (离线缓存)│ │ rrf      │ │ exporter   │  │
│ └────┬─────┘ └────┬─────┘ └────┬─────┘ └────┬─────┘ └─────┬──────┘  │
│      └────────────┴────────────┴────────────┴──────────────┘         │
│  db.py（SQLite WAL + sqlite-vec + FTS5）· schema.py（版本化迁移）     │
│  crypto/backup/privacy（M9）· logging（旋转）· single_instance        │
└──────────────────────────────────────────────────────────────────────┘
               数据根目录（单一，可迁移）：images/ thumbnails/ db/ models/ backups/ exports/ logs/
```

## 2. 数据流（核心公式）

```
截图采集（入口）
  → SHA-256 去重落盘（原图只读，命名模板 {date}_{time}_{source}.{ext}）
  → jobs 队列：ocr（幂等，重试 3 次指数退避）
      → OCR 结果：分块写 ocr_texts + ocr_fts(FTS5 trigram)，资产标记 done
      → 入队 embed → bge 向量写 embeddings + vec_embeddings(sqlite-vec)
      → 入队 thumb → 原子写缩略图
  → 混合检索（价值出口）：FTS5 关键词 + 向量语义 → RRF(k=60) 融合 → 过滤 → <mark> 高亮
  → 标注（图层 JSON）/ 打码（导出时栅格化）/ 导出（图片+CSV+PDF）
```

## 3. 模块职责

| 模块 | 职责 | 关键不变量 |
|---|---|---|
| schema.py | 表结构 + `PRAGMA user_version` 版本化迁移 | 每次迁移独立事务；SCHEMA_VERSION=1 |
| db.py | 连接管理（线程本地）、WAL、外键、事务上下文 | 多步写必须走 `tx()` |
| jobs.py | 异步任务队列 | claim 原子、幂等入队、失败重试、reset_running |
| importer.py | 批量导入/目录导入/去重/隔离 | 原图只读；重复合并保留元数据 |
| ocr_engine.py | RapidOCR 惰性加载（线程安全） | 模型离线随包 |
| embedding_engine.py | fastembed 本地推理（缓存目录） | 查询侧加 bge 指令前缀 |
| pipeline.py | ocr→FTS+embed 队列；embed→vec；thumb | 处理器幂等（先清后写） |
| search.py | 混合检索 + 过滤 + 片段高亮 + 以图搜图 | RRF k=60；短查询退化 LIKE |
| metadata.py | 标签/备注/自定义字段/回收站 | 软删 30 天；标签层级 |
| annotations.py | 图层模型/撤销重做/栅格化 | 原图字节不变 |
| exporter.py | 图片+CSV+PDF 导出 | 唯一包名；坏图隔离；临时目录 rename |
| crypto.py | AES-256-GCM 分块加解密 | PBKDF2 600k；AAD=块序号 |
| backup.py | 快照/保留 7 份/整库导出导入 | SQLite 在线备份一致快照 |
| privacy.py | 网络探针 + 静态 import 扫描 | 启动自检写日志 |
| engine.py | 外观层（服务装配 + 惰性模型） | CLI/UI/测试共用入口 |

## 4. 关键表

见 [schema.md](schema.md)。

## 5. 并发与资源

- OCR/embedding 任务限并发 = max_workers（默认 CPU 核数-1），UI 全程可响应；
- WAL 模式支持读写并发；每线程独立连接；
- 日志旋转：单文件 ≤50MB，保留 5 份。

## 6. 测试体系

| 套件 | 覆盖 | 入口 |
|---|---|---|
| core/tests（100+） | 数据层/检索融合/去重/命名/标注等单测 | `make unit` |
| tests/integration | 导入→OCR→检索→标注→导出 全链路 | `make e2e` |
| tests/quality | 240 张合成图 Top-5 ≥95% | `make quality` |
| tests/perf | 万级检索 P95≤300ms、1000 张导入 | `make perf` |
| tests/stability | kill -9 恢复 | `make stability` |
| tests/recovery | 坏库/坏图降级修复 | `make recovery` |
| coverage | ≥80%（核心模块 ≥90%） | `make coverage` |
