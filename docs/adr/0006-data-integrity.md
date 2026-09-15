# ADR-0006: 数据一致性工程 — 原子落盘 / 单实例 / 崩溃恢复

- 状态：已采纳

## 决策
1. **原子落盘**：所有文件写入走"临时文件 + os.replace/rename"（`util.atomic_write`）；
   数据库 WAL 模式 + `isolation_level=None` 禁隐式事务，多步写强制走 `Database.tx()`；
2. **单实例锁**：数据根目录锁文件 + fcntl 互斥，防止多开损坏 DB；
3. **任务队列**：jobs 表原子 claim（`UPDATE ... RETURNING`）、幂等入队（同 asset+type 的
   pending/running 跳过）、失败 3 次指数退避、启动时 `reset_running` 崩溃续扫；
   处理器幂等（先清旧数据再写），kill -9 后重启可收敛到一致状态（stability 测试证明）；
4. **坏数据隔离**：导入/导出/重建对损坏图片逐条隔离报错，不阻断批次；
   损坏数据库打开即抛 `DatabaseError`，恢复通道（快照恢复）不依赖当前库健康。

## 验证
- stability：OCR 提交前 kill -9，重启后无孤儿文件、无半写入、任务自动恢复、检索可用；
- recovery：坏库可被快照恢复、坏图导入/导出/重建均隔离。
