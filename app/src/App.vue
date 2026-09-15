<template>
  <div class="app">
    <!-- 首次启动引导 -->
    <div v-if="step === 'guide'" class="guide">
      <h1>SnapVault</h1>
      <p>本地截图资产管理系统 · 全离线 · 原图只读</p>
      <div class="field">
        <label>数据根目录</label>
        <input v-model="dataDir" placeholder="如 ~/SnapVault" />
      </div>
      <div class="field">
        <label>全屏截图快捷键</label>
        <input v-model="hkFull" />
      </div>
      <div class="field">
        <label>区域截图快捷键</label>
        <input v-model="hkRegion" />
      </div>
      <button class="primary" @click="init">初始化并进入</button>
    </div>

    <!-- 主界面 -->
    <div v-else class="main">
      <header class="topbar">
        <div class="logo">SnapVault</div>
        <input class="q" v-model="query" @keyup.enter="doSearch"
               placeholder="搜索画面文字 / 语义描述…（回车检索）" />
        <button @click="doSearch">检索</button>
        <button class="primary" @click="importDir">导入目录</button>
        <button @click="doExport">导出</button>
        <button @click="showSettings = !showSettings">⚙ 设置</button>
      </header>

      <div class="body">
        <!-- 左：标签树 -->
        <aside class="left">
          <div class="pane-title">标签</div>
          <ul class="tree">
            <li v-for="t in tags" :key="t.name"
                :class="{ active: filterTag === t.name }"
                @click="filterTag = t.name; doSearch()">
              <span class="dot" :style="{ background: t.color || '#999' }"></span>{{ t.name }}
            </li>
          </ul>
          <div class="pane-title">筛选</div>
          <label class="row"><input type="checkbox" v-model="onlyTrashed" @change="doSearch" /> 显示回收站</label>
        </aside>

        <!-- 中：缩略图瀑布流（虚拟滚动：固定行高窗口化渲染） -->
        <section class="mid">
          <div class="grid" @scroll.passive="onScroll">
            <div v-for="(a, i) in visible" :key="a.id"
                 :class="['card', { sel: selected && a.id === selected.id }]"
                 :style="{ top: i * (thumbH + gap) + 'px' }"
                 @click="select(a)">
              <img :src="thumbUrl(a)" loading="lazy" />
              <div class="meta">{{ a.id }} · {{ (a.created_at || '').slice(5,16) }}</div>
            </div>
            <div :style="{ height: (results.length * (thumbH + gap)) + 'px' }"></div>
          </div>
        </section>

        <!-- 右：详情 -->
        <aside class="right" v-if="selected">
          <img :src="thumbUrl(selected, true)" class="big" />
          <div class="pane-title">OCR 文本</div>
          <p class="ocr" v-html="ocrHtml"></p>
          <div class="pane-title">标签</div>
          <div class="chips">
            <span v-for="t in selTags" :key="t" class="chip">{{ t }}</span>
          </div>
          <div class="pane-title">备注（Markdown）</div>
          <textarea v-model="noteText" rows="4" @change="saveNote"></textarea>
          <div class="pane-title">操作</div>
          <div class="row">
            <button @click="doTrash">回收站</button>
            <button @click="doRestore">恢复</button>
          </div>
          <div class="pane-title">标注（打码演示）</div>
          <div class="row">
            <button @click="addMosaic">添加马赛克</button>
            <button @click="saveAnno">保存标注</button>
          </div>
        </aside>
        <div v-else class="right empty">← 选择一张截图</div>
      </div>

      <!-- 设置抽屉 -->
      <div v-if="showSettings" class="settings">
        <h3>设置</h3>
        <div class="field"><label>数据根目录</label><input v-model="dataDir" /></div>
        <div class="field"><label>OCR 开关</label>
          <input type="checkbox" v-model="ocrEnabled" /></div>
        <div class="field"><label>备份保留份数</label>
          <input type="number" v-model.number="backupKeep" min="1" max="30" /></div>
        <div class="field"><label>存储占用</label>
          <span>{{ storageInfo }}</span></div>
        <div class="row">
          <button class="primary" @click="saveSettings">保存</button>
          <button @click="doBackup">立即备份</button>
          <button @click="showSettings = false">关闭</button>
        </div>
      </div>

      <div class="status">{{ status }}</div>
    </div>
  </div>
</template>

<script setup>
import { ref, computed, onMounted } from "vue";

const step = ref("guide");
const dataDir = ref("~/SnapVault");
const hkFull = ref("Ctrl+Shift+1");
const hkRegion = ref("Ctrl+Shift+2");
const showSettings = ref(false);
const ocrEnabled = ref(true);
const backupKeep = ref(7);
const storageInfo = ref("");
const query = ref("");
const results = ref([]);
const selected = ref(null);
const selTags = ref([]);
const noteText = ref("");
const tags = ref([]);
const filterTag = ref("");
const onlyTrashed = ref(false);
const status = ref("");
const thumbH = 140, gap = 10;
const scrollTop = ref(0);
const visibleCount = 30;

const visible = computed(() => {
  const start = Math.floor(scrollTop.value / (thumbH + gap));
  return results.value.slice(start, start + visibleCount);
});

const API = () => {
  if (window.__TAURI__) {
    return { invoke: (c, a) => window.__TAURI__.core.invoke(c, a) };
  }
  const base = "http://127.0.0.1:8765/api";
  return { invoke: async (c, a) => {
    const r = await fetch(base + "/" + c, {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify(a || {}),
    });
    return r.json();
  }};
};

function thumbUrl(a, big = false) {
  // Tauri 模式与 Web 演示模式统一走本机 serve 通道
  return `http://127.0.0.1:8765/api/thumb?id=${a.id}&big=${big ? 1 : 0}&t=${a.updated || ""}`;
}

async function init() {
  const api = API();
  await api.invoke("init", { dataDir: dataDir.value, hkFull: hkFull.value, hkRegion: hkRegion.value });
  step.value = "main";
  refresh();
}

async function refresh() {
  const api = API();
  tags.value = await api.invoke("tags");
  const s = await api.invoke("stats");
  storageInfo.value = s ? `资产 ${s.assets} · 数据库 ${Math.round((s.db_size||0)/1024/1024*10)/10}MB` : "";
}

async function doSearch() {
  const api = API();
  status.value = "检索中…";
  const r = await api.invoke("search", {
    query: query.value, tag: filterTag.value, trashed: onlyTrashed.value, k: 200,
  });
  results.value = (r || []).map((x, i) => ({ ...x, id: x.asset_id, updated: Date.now() + i }));
  selected.value = results.value[0] || null;
  if (selected.value) await select(selected.value);
  status.value = `${results.value.length} 条结果`;
}

async function select(a) {
  selected.value = a;
  const api = API();
  const d = await api.invoke("detail", { assetId: a.id });
  if (!d) return;
  selTags.value = d.tags || [];
  noteText.value = d.note || "";
  selected.value.ocr = d.ocr || "";
  selected.value.updated = Date.now();
}

const ocrHtml = computed(() => {
  const q = (query.value || "").trim();
  let t = (selected.value?.ocr || "").replace(/</g, "&lt;");
  if (q) {
    const esc = q.replace(/[.*+?^${}()|[\]\\]/g, "\\$&");
    t = t.replace(new RegExp(esc, "gi"), (m) => `<mark>${m}</mark>`);
  }
  return t;
});

async function importDir() {
  const api = API();
  const path = prompt("请输入要导入的目录路径：");
  if (!path) return;
  status.value = "导入中…";
  await api.invoke("import_dir", { path });
  await api.invoke("process");
  status.value = "导入完成，OCR/向量已处理";
  doSearch();
}

async function doExport() {
  const api = API();
  const r = await api.invoke("export", { include: "annotated", pdf: true });
  status.value = `导出完成: ${r?.package_dir || ""}`;
  alert(`导出完成\n${r?.package_dir || ""}`);
}

async function doTrash() {
  if (!selected.value) return;
  await API().invoke("trash", { assetId: selected.value.id });
  doSearch();
}
async function doRestore() {
  if (!selected.value) return;
  await API().invoke("restore", { assetId: selected.value.id });
  doSearch();
}
async function saveNote() {
  if (!selected.value) return;
  await API().invoke("save_note", { assetId: selected.value.id, content: noteText.value });
}
async function addMosaic() {
  if (!selected.value) return;
  await API().invoke("add_mosaic", { assetId: selected.value.id });
  status.value = "已添加马赛克标注（导出时生效）";
}
async function saveAnno() {
  if (!selected.value) return;
  await API().invoke("save_anno", { assetId: selected.value.id });
  status.value = "标注已保存";
}
async function doBackup() {
  const r = await API().invoke("backup");
  status.value = "备份完成: " + (r?.path || "");
}
async function saveSettings() {
  await API().invoke("settings", { dataDir: dataDir.value, ocr: ocrEnabled.value, backupKeep: backupKeep.value });
  status.value = "设置已保存";
}
function onScroll(e) { scrollTop.value = e.target.scrollTop; }

onMounted(async () => {
  const api = API();
  const params = new URLSearchParams(location.search);
  if (params.get("q")) {
    query.value = params.get("q");
  }
  try {
    const cfg = await api.invoke("get_config");
    if (cfg && cfg.initialized) {
      dataDir.value = cfg.dataDir;
      hkFull.value = cfg.hkFull; hkRegion.value = cfg.hkRegion;
      ocrEnabled.value = cfg.ocr; backupKeep.value = cfg.backupKeep;
      step.value = "main";
      refresh();
      doSearch();
    }
  } catch (_) { /* 未初始化 → 引导 */ }
});
</script>

<style scoped>
.app { height: 100%; display: flex; flex-direction: column; }
.guide { margin: auto; width: 420px; padding: 32px; background: var(--panel);
         border-radius: 12px; border: 1px solid var(--border); }
.guide h1 { font-size: 26px; margin-bottom: 4px; }
.guide p { color: var(--muted); margin-bottom: 20px; }
.field { margin-bottom: 12px; display: flex; align-items: center; gap: 8px; }
.field label { width: 130px; color: var(--muted); flex-shrink: 0; }
.field input, .field textarea { flex: 1; }
.topbar { display: flex; gap: 8px; align-items: center; padding: 10px 14px;
          background: var(--panel); border-bottom: 1px solid var(--border); }
.logo { font-weight: 700; font-size: 17px; margin-right: 8px; color: var(--accent); }
.q { flex: 1; max-width: 520px; }
.body { flex: 1; display: grid; grid-template-columns: 200px 1fr 360px; min-height: 0; }
.left { border-right: 1px solid var(--border); overflow: auto; padding: 10px; background: var(--panel); }
.pane-title { font-weight: 600; margin: 10px 0 6px; color: var(--muted); font-size: 12px; }
.tree { list-style: none; }
.tree li { padding: 5px 8px; border-radius: 6px; cursor: pointer; }
.tree li:hover { background: var(--hover); }
.tree li.active { background: var(--accent); color: #fff; }
.dot { display: inline-block; width: 8px; height: 8px; border-radius: 50%; margin-right: 6px; }
.mid { overflow: hidden; position: relative; }
.grid { position: relative; height: 100%; overflow: auto; padding: 10px; }
.card { position: absolute; width: 210px; background: var(--panel); border: 1px solid var(--border);
        border-radius: 8px; overflow: hidden; cursor: pointer; }
.card.sel { border-color: var(--accent); box-shadow: 0 0 0 2px rgba(24,119,242,.25); }
.card img { width: 100%; height: 130px; object-fit: cover; display: block; }
.card .meta { padding: 4px 8px; font-size: 11px; color: var(--muted); }
.right { overflow: auto; padding: 12px; border-left: 1px solid var(--border); background: var(--panel); }
.right.empty { color: var(--muted); display: flex; align-items: center; justify-content: center; }
.big { width: 100%; border-radius: 8px; border: 1px solid var(--border); }
.ocr { white-space: pre-wrap; background: var(--bg); border-radius: 6px; padding: 8px; font-size: 12px; }
.chips { display: flex; flex-wrap: wrap; gap: 6px; }
.chip { background: var(--hover); border-radius: 999px; padding: 2px 10px; font-size: 12px; }
.row { display: flex; gap: 8px; align-items: center; margin: 6px 0; }
.settings { position: fixed; right: 16px; top: 60px; width: 340px; background: var(--panel);
            border: 1px solid var(--border); border-radius: 10px; padding: 16px; z-index: 50;
            box-shadow: 0 8px 30px rgba(0,0,0,.18); }
.status { padding: 4px 14px; font-size: 12px; color: var(--muted); border-top: 1px solid var(--border); }
</style>
