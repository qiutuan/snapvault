# SnapVault — 本地截图资产管理系统

> 截图采集 → 自动 OCR + 向量化 → 语义/关键词检索 → 标注/打码/导出，**全流程本地运行，零网络请求**。

面向测试人员、产品经理、售后工程师：把散落的截图变成**可检索、可标注、可导出、可迁移**的本地资产库。
图片永不修改原图；所有数据集中在一个可迁移的数据根目录。

## 核心能力

| 能力 | 说明 |
|---|---|
| 采集 | 全局快捷键全屏/区域截图、拖拽/文件夹批量导入、SHA-256 去重合并 |
| OCR | RapidOCR（PaddleOCR ONNX 模型离线打包，中英），异步任务队列，幂等 + 3 次指数退避重试 + 崩溃断点续扫 |
| 向量化 | bge-small-zh-v1.5（ONNX 本地推理，512 维），sqlite-vec 近似检索 |
| 混合检索 | FTS5 trigram 关键词 + 向量语义 + RRF(k=60) 融合；标签/时间/自定义字段/尺寸/置信度过滤；命中片段高亮；以图搜图 |
| 元数据 | 层级标签、Markdown 备注（进全文索引）、自定义字段、回收站（30 天软删） |
| 标注 | 画笔/箭头/矩形/椭圆/文字/马赛克/高斯模糊，独立图层 JSON，撤销重做 20 步，导出时栅格化 |
| 导出 | 标注版/原图目录 + CSV 汇总 + PDF 图文报告；整库导出/导入迁移包 |
| 隐私备份 | AES-256-GCM 加密快照（PBKDF2 派生口令）、每日快照保留 7 份、网络拦截自检报告 |

## 快速开始

### 1. 安装依赖（Python ≥ 3.10）

```bash
pip install -e ".[dev]"
```

### 2. 一次性下载 embedding 模型（仅首次；此后运行时完全离线）

```bash
python3 scripts/download_models.py --data-dir ~/SnapVault
```

OCR 模型已随 `snapvault-core` 包分发（`core/snapvault/models/rapidocr/*.onnx`）。

### 3. CLI 使用（无需 UI 即可完成全部核心操作）

```bash
export SNAPVAULT_DATA_DIR=~/SnapVault
snapvault init                                   # 初始化数据根目录 + 网络探测自检
snapvault import ./截图 --dir --process          # 目录批量导入并跑 OCR/向量任务
snapvault search "支付成功" --k 10               # 混合搜索
snapvault similar 3                              # 以图搜图
snapvault tag set 3 项目A/登录页                 # 打标签
snapvault note set 3 "第 3 次登录失败"           # Markdown 备注
snapvault field define defect_id 缺陷ID          # 自定义字段
snapvault annotate save 3 anno.json              # 保存标注（图层 JSON）
snapvault export --include annotated --pdf       # 导出（标注版 + CSV + PDF）
snapvault rebuild --force                        # 从 db+图片离线重建索引
snapvault backup --passphrase xxx                # 加密快照
snapvault privacy-check                          # 隐私自检
snapvault verify-integrity                       # 数据库完整性
```

完整命令见 [docs/cli.md](docs/cli.md)。

### 4. 桌面应用（Tauri 2）

```bash
cd app
npm install
npm run tauri dev        # 开发
npm run tauri build      # 打包安装包（需 Rust 工具链 + 平台 GUI 依赖）
```

桌面端依赖 `snapvault` CLI（作为 sidecar 或 PATH 中的可执行文件）。

## 项目结构

```
core/                      # 纯逻辑层（与 UI 解耦，可独立 CLI 运行）
  snapvault/
    schema.py db.py        # 数据层：版本化迁移 / WAL / 事务
    ocr_engine.py          # RapidOCR 封装（离线模型随包分发）
    embedding_engine.py    # bge-small-zh-v1.5 本地推理
    importer.py jobs.py pipeline.py   # 采集 / 任务队列 / OCR→FTS+向量管线
    search.py rrf.py       # 混合检索：FTS5 + 向量 + RRF
    metadata.py annotations.py exporter.py   # 元数据 / 图层标注 / 导出
    crypto.py backup.py privacy.py           # 加密 / 快照迁移 / 隐私自检
    engine.py cli.py       # 外观层 / CLI 入口
  tests/                   # core 单元测试（100+ 项）
tests/                     # 专项测试：integration/quality/perf/stability/recovery
app/                       # Tauri 2 桌面外壳（Vue3 前端 + Rust）
scripts/                   # 离线模型下载等
docs/                      # 架构 / ADR / schema / CLI 文档
reports/                   # 测试报告（质量/压测/稳定性/恢复/覆盖率）
```

## 测试

```bash
make unit        # core 单元测试
make quality     # 检索质量（240 图 Top-5 ≥95%）
make perf        # 万级检索 P95≤300ms + 1000 张导入
make stability   # OCR 中途 kill -9 恢复
make recovery    # 坏库/坏图降级与修复
make coverage    # 覆盖率报告（≥80%）
make e2e         # 全链路集成
make test        # 全部
```

报告输出到 `reports/`。

## 关键设计（详见 docs/adr/）

1. **技术选型**：Python core + Tauri 壳 —— 性能热点在 OCR/向量推理，Python 生态离线能力完整。
2. **OCR 选型**：RapidOCR（PaddleOCR ONNX，无 paddle 重依赖），模型随包离线分发。
3. **混合检索融合**：FTS5 trigram + 向量 + RRF(k=60)，关键词精确 + 语义召回互补。
4. **图层标注**：JSON 图层 + 导出时栅格化，原图字节级只读。
5. **备份与加密**：SQLite 在线备份快照 + AES-256-GCM 分块加密（PBKDF2）。

## 数据目录

```
~/SnapVault/
  images/        # 原图（只读，永不修改）
  thumbnails/    # 缩略图（可从原图重建）
  db/            # SQLite 主库（WAL）
  models/        # fastembed 模型缓存（一次性下载）
  logs/          # 旋转日志（单文件≤50MB，保留 5 个）
  backups/       # 每日快照（保留最近 7 份）
  exports/       # 导出包
  config.json    # 用户配置
```

整体迁移：`snapvault export-library` → 拷贝 zip → 目标机 `snapvault import-library`。

## 隐私承诺

- 运行时**零网络请求**（启动时网络探测自检并写日志；`privacy-check` 输出拦截测试报告）。
- 图片永不修改原图；标注/打码为图层叠加。
- 可选 AES-256-GCM 加密存储（口令 PBKDF2 派生，600k 次迭代）。
- 所有落盘原子化（临时文件 + rename），数据库 WAL + 事务。

## License

MIT
