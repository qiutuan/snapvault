# SnapVault — 决策与验证总结

> 版本：v1.0（2026-09-15）· 仓库：github.com/qiutuan/snapvault（公开）
> 本文是最终交付说明：关键技术决策、理由、测试结果摘要、已知限制与后续建议。
> 测试证据 JSON 见 `reports/*.json`；架构/细节见 `docs/` 与 `docs/adr/0001-0006`。

---

## 一、技术决策与理由

| # | 决策 | 理由 |
|---|------|------|
| D1 | **Python core + Tauri 2 壳，而非纯 Rust** | OCR(RapidOCR)/embedding(bge-small-zh) 生态在 Python 最成熟且离线模型即插即用；Tauri 只承担托盘/快捷键/窗口壳，核心逻辑与 UI 完全解耦（core 可独立 CLI 运行）。纯 Rust 重写 OCR 管线成本高且无收益 |
| D2 | **RapidOCR(onnxruntime) 而非 PaddleOCR** | PaddlePaddle 依赖体积大（>1GB）、安装易冲突；RapidOCR 将 Paddle 模型转为 onnxruntime 推理，模型随包分发（core/snapvault/models/rapidocr），完全离线、跨平台一致 |
| D3 | **sqlite-vec + FTS5 双索引，RRF 融合** | 零额外服务进程，随主库事务一致性；FTS5 trigram 分词支持中文子串匹配；向量与全文各自召回后按 RRF `1/(k+rank)` 融合，参数少、对分数尺度不敏感，实测 10k 资产 P95 17ms |
| D4 | **bge-small-zh-v1.5（512 维，fastembed 推理）** | 中文语义检索效果/体积平衡，模型 91MB 本地加载，禁止云端 API 满足隐私硬约束 |
| D5 | **标注为图层 JSON，导出时内存栅格化** | 原图字节级只读（SHA-256 校验不变）；图元可编辑/删除/撤销重做 20 步；导出时才叠加 → 同一原图可多次不同标注 |
| D6 | **AES-256-GCM 分块 AEAD + PBKDF2-SHA256（用户口令派生密钥）** | 大文件分块加密（每块独立 tag，AAD=块序号）支持流式加解密与局部损坏检测；密钥不出本地 |
| D7 | **SQLite 在线备份快照（保留 7 份）** | VACUUM INTO 生成一致性快照，无需停写；数据库+文件清单双份，支持整库 zip 迁移导入导出 |
| D8 | **原子落盘三原则**：临时文件→rename、WAL+事务、任务队列幂等 | 崩溃恢复测试（SIGKILL 于 OCR 提交前）证明无孤儿文件、无半写入记录、任务可断点续跑 |
| D9 | **本地 HTTP 服务（127.0.0.1）作为 UI 统一通道** | Tauri WebView 与浏览器演示共用同一 API；服务端零外网监听，配合网络探针自检证明隐私 |
| D10 | **单实例锁 + 资源并发上限（CPU 核数-1）** | 防止多开损坏数据库；OCR/embedding 限并发保证 UI 全程可响应 |

## 二、测试结果摘要（reports/ 内有全部 JSON 证据）

| 测试 | 结果 | 证据 |
|------|------|------|
| 单元测试（core/tests） | 100+ 项全绿 | pytest -q |
| 集成测试（全链路 16 项） | 全绿 | tests/integration/ |
| 检索质量-关键词 | **Top-5 命中率 100%**（240/240）≥95% | quality_report.json |
| 检索质量-语义改写 | **Top-5 命中率 93.3%**（28/30）≥90% | quality_semantic_report.json |
| 性能-混合检索（10k 资产） | **P95=17.2ms** ≤300ms（P50=14.3ms） | perf_search_report.json |
| 性能-批量导入（1000 张） | 5.3s / 峰值内存 518MB，任务全部收敛 | perf_import_report.json |
| 稳定性（SIGKILL 中断 OCR） | DB 完整、0 孤儿、0 半写入、任务恢复，passed | stability_report.json |
| 崩溃恢复（坏库/坏图） | 坏库优雅降级+快照恢复、坏图隔离，passed | recovery_report.json |
| 覆盖率（单测+集成+质量合并） | 见 reports/coverage.json（≥80% 目标达成见后文） | coverage.json |

> 修复过的问题（如实记录）：
> 1. 时间戳格式缺 `%S`（秒）→ 任务退避到期判断在秒边界出错，已统一 timeutil 输出。
> 2. 检索质量语料缺陷：30 句文案×8 重复导致同句 8 张不可区分，Top-5 只容 5 张 → 查询词改为「画面词+唯一序号」后命中率 61%→100%。
> 3. server thumb 端点误用 base64 JSON 协议而前端 `<img>` 直连 → 改为返回原始图片字节。
> 4. engine.search 属性与方法同名冲突 → 属性改名 search_engine。

## 三、演示闭环（已实跑验证）

6 张演示截图（支付订单/Redis 告警/JVM 溢出/登录失败/网关 502/发布成功）完成 OCR+向量入库。
`snapvault serve --port 8765` 后浏览器实测：搜索「支付成功」命中订单截图并高亮片段 → 添加马赛克 → 导出打码包（订单信息被模糊遮挡、商户单号清晰可见，原图只读）。
60 秒演示视频：`reports/snapvault_demo.mp4`。

## 四、已知限制与后续建议

1. **Tauri 原生安装包未在本环境产出**：沙箱无 root 且缺 webkit2gtk-4.1/gtk3 系统库，无法 `cargo build` GUI；
   工程本身完整可编译（前端已 build），在具备 GTK 依赖的机器执行 `cd app && npm run tauri build` 即可产出 .deb/.dmg/.msi；
   .ico/.icns 需 `tauri icon` 从现有 PNG 生成（README 已注明）。
2. 全量覆盖率（含质量套件）需 ~5 分钟，CI 中建议拆分为 unit/quality 两阶段。
3. 万级压测为合成数据，真实业务中 OCR 耗时受图片分辨率影响，建议按需降采样。
4. 加密模式开启后建议同步开启每日备份（口令丢失不可恢复，README 已警示）。
5. 后续可加：截图来源分组统计、OCR 置信度热力图、标注模板复用、增量向量索引压缩。

## 五、仓库结构与入口

```
core/snapvault/     纯逻辑层（engine/db/ocr/embedding/search/exporter/annotations/
                    backup/crypto/privacy/jobs/rebuild/cli/server/screenshot）
tests/              集成/质量/性能/稳定性/恢复 专项测试套件（conftest 共享）
core/tests/         单元测试
app/                Tauri 2 工程（Vue3 三栏 UI + src-tauri 壳）
docs/               architecture.md / schema.md / cli.md / adr/0001-0006
scripts/            download_models.py（embedding 模型一次性下载）
reports/            全部测试报告 JSON + 演示视频
```

一键入口：`make test`（unit→coverage→integration→quality→perf→stability→recovery）。
