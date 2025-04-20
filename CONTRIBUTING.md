# 项目贡献指南

欢迎参与 AsukaNeko 项目！以下是贡献代码的指南。

## 开始之前

1. 确保你已阅读并同意遵守 [行为准则](CODE_OF_CONDUCT.md)
2. 检查 [Issues](https://github.com/yourname/AsukaNeko/issues) 看是否已有相关讨论

## 贡献流程

### 报告问题
- 在提交新 Issue 前请先搜索是否已有类似问题
- 提供清晰的问题描述、复现步骤和预期行为
- 如果是 Bug，请注明环境信息（Python版本、操作系统等）

### 提交 Pull Request
1. Fork 本项目
2. 创建你的功能分支 (`git checkout -b feature/AmazingFeature`)
3. 提交你的更改 (`git commit -m 'Add some AmazingFeature'`)
4. 推送到分支 (`git push origin feature/AmazingFeature`)
5. 打开一个 Pull Request

## 开发规范

### 代码风格
- 遵循 PEP 8 规范
- 使用类型注解 (Type Hints)
- 函数和类要有 docstring

### 提交信息
- 使用英文撰写提交信息
- 采用 [约定式提交](https://www.conventionalcommits.org/) 格式
- 示例: `feat: add new command handler`

## 测试要求
- 新功能需要包含单元测试
- 通过所有现有测试 (`pytest tests/`)
- 代码覆盖率不应低于 80%

## 环境设置
```bash
# 克隆项目
git clone https://github.com/yourname/AsukaNeko.git
cd AsukaNeko

# 安装依赖
pip install -r requirements.txt
```