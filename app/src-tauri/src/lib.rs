//! SnapVault Tauri 壳：窗口 / 托盘 / 单实例 / 全局快捷键。
//!
//! 集成方式：应用启动时后台拉起 `snapvault serve --port 8765`（本机回环），
//! 前端（Vue3）统一通过 http://127.0.0.1:8765/api 调用 Python core；
//! 壳本身不承载业务逻辑，保证 core 完全离线、UI 与逻辑解耦。
#![cfg_attr(not(debug_assertions), windows_subsystem = "windows")]

use serde_json::{json, Value};
use std::path::PathBuf;
use std::process::{Child, Command, Stdio};
use std::sync::Mutex;
use tauri::Manager;

pub struct ServeProcess(pub Mutex<Option<Child>>);

/// 读取数据根目录 config.json（供前端初始化探测）。
#[tauri::command]
fn get_config(data_dir: String) -> Result<Value, String> {
    let root = PathBuf::from(data_dir);
    let cfg = root.join("config.json");
    if cfg.exists() {
        let text = std::fs::read_to_string(&cfg).map_err(|e| e.to_string())?;
        let v: Value = serde_json::from_str(&text).unwrap_or(json!({}));
        Ok(json!({
            "initialized": true,
            "dataDir": root.to_string_lossy(),
            "hkFull": v.pointer("/hotkeys/fullscreen").and_then(Value::as_str).unwrap_or("Ctrl+Shift+1"),
            "hkRegion": v.pointer("/hotkeys/region").and_then(Value::as_str).unwrap_or("Ctrl+Shift+2"),
            "ocr": v.pointer("/ocr_enabled").and_then(Value::as_bool).unwrap_or(true),
            "backupKeep": v.pointer("/backup_keep").and_then(Value::as_u64).unwrap_or(7),
        }))
    } else {
        Err("not_initialized".into())
    }
}

/// 初始化数据根目录并拉起 serve。
#[tauri::command]
fn init(app: tauri::AppHandle, data_dir: String) -> Result<Value, String> {
    std::fs::create_dir_all(&data_dir).map_err(|e| e.to_string())?;
    start_serve(&app, &data_dir);
    Ok(json!({ "initialized": true }))
}

fn start_serve(app: &tauri::AppHandle, data_dir: &str) {
    let mut guard = app.state::<ServeProcess>().0.lock().unwrap();
    if let Some(child) = guard.as_mut() {
        if child.try_wait().ok().flatten().is_none() {
            return; // 已在运行
        }
    }
    let child = Command::new("snapvault")
        .args(["serve", "--port", "8765", "--data-dir", data_dir])
        .env("SNAPVAULT_DATA_DIR", data_dir)
        .stdin(Stdio::null())
        .stdout(Stdio::null())
        .stderr(Stdio::null())
        .spawn()
        .ok();
    *guard = child;
}

#[cfg(target_os = "linux")]
fn setup_tray(app: &tauri::AppHandle) -> tauri::Result<()> {
    use tauri::menu::{MenuBuilder, MenuItemBuilder};
    use tauri::tray::TrayIconBuilder;

    let open = MenuItemBuilder::with_id("open", "打开主界面").build(app)?;
    let shot = MenuItemBuilder::with_id("shot", "区域截图并入库").build(app)?;
    let quit = MenuItemBuilder::with_id("quit", "退出").build(app)?;
    let menu = MenuBuilder::new(app)
        .item(&open).item(&shot).separator().item(&quit)
        .build()?;

    let icon = app
        .default_window_icon()
        .cloned()
        .ok_or_else(|| tauri::Error::AssetNotFound("icon".into()))?;
    TrayIconBuilder::with_id("snapvault-tray")
        .icon(icon)
        .menu(&menu)
        .show_menu_on_left_click(true)
        .on_menu_event(|app, event| match event.id().as_ref() {
            "open" => {
                if let Some(w) = app.get_webview_window("main") {
                    let _ = w.show();
                    let _ = w.unminimize();
                    let _ = w.set_focus();
                }
            }
            "shot" => {
                let _ = Command::new("snapvault").args(["screenshot", "--import"]).spawn();
            }
            "quit" => app.exit(0),
            _ => {}
        })
        .build(app)?;
    Ok(())
}

#[cfg(not(target_os = "linux"))]
fn setup_tray(app: &tauri::AppHandle) -> tauri::Result<()> {
    let _ = app;
    Ok(())
}

#[cfg_attr(mobile, tauri::mobile_entry_point)]
pub fn run() {
    tauri::Builder::default()
        .manage(ServeProcess(Mutex::new(None)))
        .setup(|app| {
            use tauri::Manager;
            let _ = tauri_plugin_single_instance::init(|app, _args, _cwd| {
                if let Some(w) = app.get_webview_window("main") {
                    let _ = w.show();
                    let _ = w.unminimize();
                    let _ = w.set_focus();
                }
            });
            let handle = app.handle().clone();
            let data_dir = std::env::var("SNAPVAULT_DATA_DIR").unwrap_or_else(|_| {
                let home = std::env::var("HOME").unwrap_or_default();
                format!("{home}/SnapVault")
            });
            start_serve(&handle, &data_dir);
            setup_tray(&handle)?;
            Ok(())
        })
        .invoke_handler(tauri::generate_handler![init, get_config])
        .on_window_event(|window, event| {
            if let tauri::WindowEvent::CloseRequested { api, .. } = event {
                let _ = window.hide(); // 关闭窗口 → 隐藏到托盘
                api.prevent_close();
            }
        })
        .run(tauri::generate_context!())
        .expect("error while running SnapVault");
}
