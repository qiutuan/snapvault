"""检索质量测试（M4）：≥200 张含已知文字的合成截图。

验证两种查询模式：
1. 精确关键词：输入画面中的文字 → Top-5 命中对应截图（≥95%）
2. 语义改写：用同义改写查询 → Top-5 命中对应截图（≥90%，向量语义召回）

结果写入 reports/quality_report.json。
"""
from __future__ import annotations

import json
import random
import time
from pathlib import Path

import pytest

REPORTS = Path(__file__).resolve().parents[2] / "reports"

FONT = Path(__file__).resolve().parents[2] / "fonts" / "NotoSansSC-Regular.otf"


def _render_wrapped(text: str, width: int = 1000, height: int = 260) -> Path:
    """渲染可换行的白底黑字截图，保证全部文字可见（避免裁切丢字）。"""
    from PIL import Image, ImageDraw, ImageFont
    from tempfile import NamedTemporaryFile

    font = ImageFont.truetype(str(FONT), 34)
    lines, cur = [], ""
    for ch in text:
        if len(cur) and font.getlength(cur + ch) > width - 60:
            lines.append(cur)
            cur = ch
        else:
            cur += ch
    if cur:
        lines.append(cur)
    line_h = 44
    img = Image.new("RGB", (width, max(height, len(lines) * line_h + 60)), "white")
    d = ImageDraw.Draw(img)
    y = 24
    for ln in lines:
        d.text((24, y), ln, fill="black", font=font)
        y += line_h
    d.rectangle((24, y + 8, width - 24, y + 20), outline="gray", width=2)
    f = NamedTemporaryFile(suffix=".png", delete=False)
    f.close()
    img.save(f.name)
    return Path(f.name)

# 240 条带独特关键词的页面文案（模拟真实截图内容）
_SENTENCES = [
    "订单支付成功 金额128元 等待发货",
    "登录失败 密码错误 重试按钮",
    "Redis 集群超时 重试机制已启用",
    "JVM 堆内存溢出 排查 dump 文件",
    "API 网关 502 Bad Gateway 上游超时",
    "MySQL 主从延迟 复制状态异常",
    "用户权限不足 403 拒绝访问",
    "磁盘空间不足 清理临时文件",
    "微服务注册中心 服务下线通知",
    "消息队列积压 消费者处理缓慢",
    "前端构建失败 ESLint 语法错误",
    "数据库连接池耗尽 获取连接超时",
    "证书过期 TLS 握手失败",
    "限流策略触发 429 请求过多",
    "灰度发布 新版本 v2.3.1 已上线",
    "浏览器缓存导致 页面样式错乱",
    "埋点数据上报失败 网络波动",
    "定时任务执行异常 重试三次后放弃",
    "文件上传失败 超时大小超限",
    "验证码校验失败 请重新获取",
    "搜索服务不可用 降级为关键词匹配",
    "配置中心变更 灰度生效中",
    "日志采集异常 磁盘 IO 瓶颈",
    "图片加载失败 资源路径错误",
    "优惠券领取失败 库存不足",
    "订单状态同步异常 等待补偿",
    "支付回调重复通知 幂等处理",
    "用户会话过期 重新登录",
    "灰度流量异常 回滚到稳定版",
    "容器启动失败 镜像拉取超时",
] * 8  # 240 张

# 语义改写查询（同义表达，不含原文关键词）
_PARAPHRASES = {
    "订单支付成功 金额128元 等待发货": "付款完成账单128元待出库",
    "登录失败 密码错误 重试按钮": "账号密码不对无法进入系统",
    "Redis 集群超时 重试机制已启用": "缓存节点连接中断自动重连",
    "JVM 堆内存溢出 排查 dump 文件": "Java进程内存用尽导出快照",
    "API 网关 502 Bad Gateway 上游超时": "接口网关报错服务无响应",
    "MySQL 主从延迟 复制状态异常": "数据库同步滞后主备不一致",
    "用户权限不足 403 拒绝访问": "没有操作权限被服务器拦截",
    "磁盘空间不足 清理临时文件": "硬盘容量快满需要删临时数据",
    "微服务注册中心 服务下线通知": "服务治理平台实例移除告警",
    "消息队列积压 消费者处理缓慢": "MQ队列堆积消费速率低",
    "前端构建失败 ESLint 语法错误": "打包出错代码规范检查不过",
    "数据库连接池耗尽 获取连接超时": "数据库连接占满拿不到连接",
    "证书过期 TLS 握手失败": "SSL证书失效加密通道建立失败",
    "限流策略触发 429 请求过多": "访问频率超限被限流拦截",
    "灰度发布 新版本 v2.3.1 已上线": "分批上线新代码版本号升级",
    "浏览器缓存导致 页面样式错乱": "浏览器旧缓存让界面渲染异常",
    "埋点数据上报失败 网络波动": "统计事件发送失败网络不稳",
    "定时任务执行异常 重试三次后放弃": "调度作业失败反复尝试最终停止",
    "文件上传失败 超时大小超限": "传文件出错超过大小限制",
    "验证码校验失败 请重新获取": "图形码验证不对再取一次",
    "搜索服务不可用 降级为关键词匹配": "检索引擎挂了走基础查询",
    "配置中心变更 灰度生效中": "配置平台改动逐步推送",
    "日志采集异常 磁盘 IO 瓶颈": "日志管道故障存储读写慢",
    "图片加载失败 资源路径错误": "图片404地址不对",
    "优惠券领取失败 库存不足": "领券失败券已领完",
    "订单状态同步异常 等待补偿": "订单状态不同步在跑对账",
    "支付回调重复通知 幂等处理": "支付结果多次回调已去重",
    "用户会话过期 重新登录": "登录态失效需重新认证",
    "灰度流量异常 回滚到稳定版": "新版本流量有问题退回旧版",
    "容器启动失败 镜像拉取超时": "pod起不来镜像下载失败",
}


class TestQuality:
    def test_top5_keyword_hit_rate(self, engine, make_shot):
        texts = [f"{s} [{i:03d}]" for i, s in enumerate(_SENTENCES)]
        paths = [_render_wrapped(t) for t in texts]
        rep = engine.import_files(paths, source="quality")
        assert len(rep.imported) == len(texts), f"导入失败: {rep.failed[:3]}"
        n = engine.process_jobs()
        assert engine.job_stats().get("pending", 0) == 0

        # 精确关键词查询：每张图带唯一序号，查询 = 画面中的词 + 唯一序号
        # （30 句文案 × 8 重复时，仅靠词会使同句 8 张不可区分，Top-5 只能容纳 5 张）
        keyword = [f"{s.split()[1] if len(s.split()) > 1 else s.split()[0]} {i:03d}"
                   for i, s in enumerate(_SENTENCES)]
        hits_ok = 0
        latencies = []
        for i, kw in enumerate(keyword):
            t0 = time.perf_counter()
            hits = engine.search(kw, k=5)
            latencies.append((time.perf_counter() - t0) * 1000)
            if any(h.asset_id == rep.imported[i] for h in hits):
                hits_ok += 1
        rate = hits_ok / len(texts)
        report = {
            "suite": "quality",
            "total_images": len(texts),
            "top5_keyword_hits": hits_ok,
            "top5_keyword_rate": round(rate, 4),
            "p95_latency_ms": round(sorted(latencies)[int(len(latencies) * 0.95)], 1),
            "threshold": 0.95,
        }
        REPORTS.mkdir(exist_ok=True)
        (REPORTS / "quality_report.json").write_text(
            json.dumps(report, ensure_ascii=False, indent=2))
        assert rate >= 0.95, f"Top-5 关键词命中率 {rate:.2%} < 95%"

    def test_top5_semantic_hit_rate(self, engine, make_shot):
        """语义改写查询（向量召回为主）。"""
        texts = list(_PARAPHRASES.keys())
        paths = [_render_wrapped(t) for t in texts]
        rep = engine.import_files(paths, source="quality-sem")
        engine.process_jobs()
        hits_ok = 0
        for i, src in enumerate(texts):
            q = _PARAPHRASES[src]
            hits = engine.search(q, k=5)
            if any(h.asset_id == rep.imported[i] for h in hits):
                hits_ok += 1
        rate = hits_ok / len(texts)
        report = {
            "suite": "quality-semantic",
            "total_images": len(texts),
            "top5_semantic_hits": hits_ok,
            "top5_semantic_rate": round(rate, 4),
            "threshold": 0.90,
        }
        REPORTS.mkdir(exist_ok=True)
        (REPORTS / "quality_semantic_report.json").write_text(
            json.dumps(report, ensure_ascii=False, indent=2))
        assert rate >= 0.90, f"Top-5 语义命中率 {rate:.2%} < 90%"
