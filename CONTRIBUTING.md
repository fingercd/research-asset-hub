# 参与开发

项目代码采用 MIT 许可；资料库中的论文、代码快照及第三方依赖各自保留原许可证。

先阅读 `AGENTS.md` 和 `docs/architecture/source-boundaries.md`。已有来源文件与冻结清单只读；测试使用隔离数据库、临时文件及非敏感的小型材料，不对个人正式库执行写入。

使用 `uv sync --frozen --extra dev` 与 `pnpm install --frozen-lockfile` 准备环境。提交前执行与改动相关的 Python 测试、Ruff 与类型检查；前端执行 typecheck、test 和 build。桌面变更另外运行 Rust 测试。

修改用户流程时说明触发条件、结果、迁移影响与实际验证范围。数据库变化通过 Alembic 迁移；衍生搜索索引应可从正式数据重建。遇到已有测试失败，记录基线并说明归因，不用降低断言取得通过。
