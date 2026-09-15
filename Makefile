# SnapVault 统一测试/构建入口
PYTHON ?= python3
PIP ?= pip3
export PYTHONPATH := core

.PHONY: install test unit coverage quality perf stability recovery e2e build clean

install:
	$(PIP) install -e ".[dev]"
	bash scripts/download_models.sh

# 单元测试（core 层）
unit:
	$(PYTHON) -m pytest core/tests -q

# 覆盖率报告（core 层全量测试 >=80%，写入 reports/coverage.json）
coverage:
	$(PYTHON) -m pytest core/tests tests/integration tests/quality \
		--cov=snapvault --cov-report=term --cov-report=json:reports/coverage.json -q

# 检索质量测试（>=200 张合成图，Top-5 >=95%）
quality:
	$(PYTHON) -m pytest tests/quality -q

# 性能压测（10k 检索 P95<=300ms、1000 张导入）
perf:
	$(PYTHON) -m pytest tests/perf -q

# 稳定性测试（进程 kill 恢复）
stability:
	$(PYTHON) -m pytest tests/stability -q

# 崩溃恢复测试
recovery:
	$(PYTHON) -m pytest tests/recovery -q

# 端到端（CLI 驱动全链路 + API 面）
e2e:
	$(PYTHON) -m pytest tests/integration -q

# 全量测试
test: unit coverage quality perf stability recovery e2e

clean:
	rm -rf .pytest_cache .coverage htmlcov reports/coverage
	find . -name __pycache__ -type d -exec rm -rf {} +
