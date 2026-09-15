# 数据库 Schema 文档

- 数据库：SQLite（WAL），主库 `db/snapvault.db`
- 迁移机制：`PRAGMA user_version`，当前 `SCHEMA_VERSION = 1`；每次迁移独立事务
- 扩展：`sqlite-vec`（vec0 虚拟表，float[512]）、`FTS5`（trigram tokenizer）

## 表清单

### assets — 截图主表
| 列 | 类型 | 说明 |
|---|---|---|
| id | INTEGER PK | |
| path | TEXT NOT NULL | 原图绝对路径（只读） |
| sha256 | TEXT NOT NULL | 内容哈希（去重键） |
| width / height | INTEGER | 像素尺寸 |
| format | TEXT | PNG/JPG/WEBP/BMP |
| source | TEXT | import / 截图来源标识 |
| created_at | TEXT | UTC ISO-8601 |
| ocr_status | TEXT | pending/done/failed |
| ocr_confidence | REAL | 均值置信度 |
| deleted_at | TEXT NULL | 非空=在回收站 |

### ocr_texts — OCR 分块结果
asset_id FK, chunk_index, text, confidence, bbox(JSON), lang

### ocr_fts — FTS5 全文索引（trigram）
asset_id, chunk_index, text（contentless 镜像表）

### tags / asset_tags — 多对多标签
tags(name 唯一, color)；asset_tags(asset_id, tag_id)

### notes — Markdown 备注
asset_id 唯一, content, updated_at

### notes_fts — 备注全文索引（可配开关 `notes_in_fts`）

### embeddings — 资产级向量元数据
asset_id PK FK, embedding BLOB, model, dim

### vec_embeddings — sqlite-vec 向量索引（vec0 float[512]）
rowid=asset_id, embedding

### jobs — 异步任务队列
id, asset_id FK, type(ocr/embed/thumb), status(pending/running/done/failed),
attempts, max_attempts=3, next_retry_at, started_at, error, priority

### custom_field_defs / custom_field_values — 自定义字段
defs(key 唯一, label, type: text/single_choice/date)；values(asset_id, field_id, value)

### annotations — 标注图层
asset_id PK, model_json（版本化图层 JSON）, updated_at

### settings — 键值配置（可覆盖 config.json）

### audit_log — 关键操作留痕（导入/OCR/删除/导出/备份）
action, detail, created_at

## 索引
- assets.sha256 UNIQUE（去重）
- ocr_fts 使用 FTS5 trigram；notes_fts 同
- jobs(status, priority)、jobs(asset_id, type) 幂等查询
- asset_tags(asset_id, tag_id) 复合

## 重建
`snapvault rebuild [--force]`：从 数据库+图片文件 离线重建 ocr_fts/vec_embeddings/thumbnails，
处理器幂等（先清旧索引再写）。
