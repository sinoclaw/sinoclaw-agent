# Sinoclaw-Agent 更新指南

本项目 fork 自 [NousResearch/sinoclaw-agent](https://github.com/nousresearch/sinoclaw-agent)，用于品牌定制和二次开发。

## 如何同步上游更新

上游仓库：`https://github.com/nousresearch/sinoclaw-agent`

### 1. 拉取上游最新代码

```bash
git fetch upstream
```

### 2. 合并到你的分支

```bash
# 切换到 main 分支
git checkout main

# 合并上游更新
git merge upstream/main

# 如果有冲突，手动解决冲突后：
git add .
git commit -m "Merge upstream changes"
git push origin main
```

### 3. 解决重命名冲突

上游更新可能包含对 Hermes → Sinoclaw 的修改。如果有冲突，搜索 `Hermes`、`sinoclaw`、`sinoclaw_cli` 等关键词，确保全部替换为对应的 Sinoclaw 名称。

常见需要替换的内容：
- `Hermes` → `Sinoclaw`
- `sinoclaw` → `sinoclaw`
- `sinoclaw_constants.py` → `sinoclaw_constants.py`
- `sinoclaw_cli/` → `sinoclaw_cli/`
- `sinoclaw state` → `sinoclaw state`
- `Sinoclaw Agent` → `Sinoclaw Agent`
- `sinoclaw-agent` → `sinoclaw-agent`

### 4. 重命名文件名冲突

如果上游新增或重命名了 Hermes 相关文件，需要手动重命名：

```bash
# 示例：上游新增了 sinoclaw_util.py
mv sinoclaw_util.py sinoclaw_util.py
# 同时更新所有 import 引用
find . -name "*.py" -exec sed -i 's/sinoclaw_util/sinoclaw_util/g' {} +
```

## 自动化同步脚本

```bash
#!/bin/bash
# sync-upstream.sh

git fetch upstream
git checkout main
git merge upstream/main --no-edit

# 强制替换残留的 sinoclaw 引用
find . -type f \( -name "*.py" -o -name "*.sh" -o -name "*.md" -o -name "*.json" -o -name "*.yaml" \) \
  ! -path "./.git/*" ! -path "./website/*" \
  -exec sed -i 's/Hermes/Sinoclaw/g' {} +
find . -type f \( -name "*.py" -o -name "*.sh" -o -name "*.md" -o -name "*.json" -o -name "*.yaml" \) \
  ! -path "./.git/*" ! -path "./website/*" \
  -exec sed -i 's/sinoclaw/sinoclaw/g' {} +

git add -A
git commit -m "Sync: Hermes → Sinoclaw rename after upstream merge" || true
git push origin main
```

## 注意事项

- 上游仓库较活跃（~93k stars），建议定期同步
- 同步前先在本地测试确保功能正常
- 敏感配置（.env）不要推送到 GitHub
