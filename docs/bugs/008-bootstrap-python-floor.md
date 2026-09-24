# BUG-008 默认 python3 过老的机器自举失败（python≥3.10 地板 + PEP 660 pip 地板双重叠加）

- **发现**：2026-09-24，blur.sh Task 4 手工验收——把现成 venv 移走后 `scripts/blur.sh run`，bootstrap 即败
- **现象**：venv 缺失时 `python3 -m venv venv && ./venv/bin/pip install -e ".[dev]"` 失败，blur.sh 报错退出，spec §6「新 clone 一条命令跑通」承诺被打破；本机默认 `python3` = 3.8.5（自带 pip 20.1.1），真实 venv 用的是 `python3.11`（3.11.15）
- **根因**：双重地板叠加：(1) 项目是 pyproject-only（无 setup.py），可编辑安装依赖 PEP 660，要求 pip ≥21.3——3.8.5 自带的 pip 20.1.1 不支持，`pip install -e` 直接死；(2) 即便 pip 够新，pyproject `requires-python = ">=3.10"` 也会拒绝 python 3.8。bootstrap 处方写死 `python3`，在默认 python3 <3.10 的任何机器上必然失败——处方只按开发机（已存在新 venv，bootstrap 路径从未真正跑过）验证，没按最老现实工具链验证
- **修复**：`ensure_env` 增加 `pick_python()`（按 `python3 → python3.13 → python3.12 → python3.11 → python3.10` 顺序取首个版本 ≥3.10，找不到返回 1），选版失败 → 干净 exit 2 并提示 `brew install python@3.11`；用它创建 venv，创建后先 `pip install --upgrade pip`（拉到 ≥21.3 满足 PEP 660）再 `pip install -e ".[dev]"`。原 `command -v python3` 检查被 pick_python 取代（无任何候选 python 时同样 exit 2）
- **教训**：bootstrap 处方必须按「最老现实工具链」验证，不能因为自己开发机上有现成新 venv、自举路径从未真正执行就当作成立；环境自举里的依赖地板（解释器地板 + pip 地板）要显式编码成检查（选版 + 升级），而不是隐式假设系统默认满足
