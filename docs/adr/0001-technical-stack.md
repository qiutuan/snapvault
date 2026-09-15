# ADR-0001: 技术选型 — Python core + Tauri 2 外壳

- 状态：已采纳（2026-09）

## 背景
桌面应用需要：完全离线、OCR/embedding/向量库本地化、可独立 CLI 运行、跨平台。

## 决策
1. **核心逻辑层用 Python**（`core/`），与 UI 完全解耦，提供 CLI 入口；
2. **桌面壳用 Tauri 2**（Rust + 系统 WebView），前端 Vue 3；
3. Python 通过 sidecar 子进程方式与 Tauri 集成（壳只做窗口/托盘/快捷键/权限）。

## 理由
- 性能热点在 OCR（onnxruntime）与向量推理（fastembed），均不在 UI 壳；Python 对这些生态的离线打包最成熟（模型随包分发）。
- Tauri 体积小（无 Electron 千兆依赖）、内存占用低、托盘/全局快捷键原生支持好。
- core 纯逻辑层可独立于 UI 测试与运行——质量/压测/稳定性测试全部走 CLI/API 面，不依赖 GUI 环境。

## 后果
- core 与 UI 的边界 = CLI 命令 + 本地 HTTP API（serve 模式），UI 未就绪时功能完整可用。
- 桌面构建需要 Rust 工具链与平台 GUI 依赖（Linux: webkit2gtk-4.1）。
