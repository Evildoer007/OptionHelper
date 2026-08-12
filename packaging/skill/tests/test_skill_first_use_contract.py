"""Independent first-use audit for the portable OptionHelper Skill."""

from __future__ import annotations

import re
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[3]


class SkillFirstUseContractTests(unittest.TestCase):
    def test_readme_has_one_host_neutral_setup_path(self) -> None:
        readme = (ROOT / "packaging" / "skill" / "SKILL_README.md").read_text(encoding="utf-8")

        self.assertIn('OPTIONHELPER_SKILL_ROOT="$PWD/.claude/skills/option-helper"', readme)
        self.assertIn('"$OPTIONHELPER_SKILL_ROOT/scripts/requirements.lock"', readme)
        self.assertIn('"$OPTIONHELPER_SKILL_ROOT/scripts/environment_check.py" --check-dependencies', readme)
        self.assertIn('"$OPTIONHELPER_SKILL_ROOT/scripts/environment_check.py" --check-store', readme)
        self.assertIn('"$OPTIONHELPER_SKILL_ROOT/scripts/tool_entry.py" --list', readme)
        self.assertNotIn("-m pip install -r .claude/", readme)

    def test_native_agent_does_not_request_a_second_model_configuration(self) -> None:
        readme = (ROOT / "packaging" / "skill" / "SKILL_README.md").read_text(encoding="utf-8")

        self.assertIn("不要再配置模型地址、模型名称或模型API Key", readme)
        self.assertIn("当前Agent本身就是对话模型", readme)
        self.assertIn("只有在没有现成对话模型", readme)
        self.assertIn("IFIND_REFRESH_TOKEN", readme)
        self.assertIn("不要求用户维护Access Token", readme)
        self.assertIn("仅当任务需要获取新数据时", readme)
        self.assertIn("基于当前任务已有已验证结果生成交付物，也不重新检查数据凭据", readme)
        self.assertIsNone(re.search(r"\bsk-[A-Za-z0-9]{16,}\b", readme))

    def test_python_environment_is_confirmed_once_before_execution(self) -> None:
        readme = (ROOT / "packaging" / "skill" / "SKILL_README.md").read_text(encoding="utf-8")
        skill = (ROOT / "SKILL.md").read_text(encoding="utf-8")

        for text in (skill, readme):
            self.assertIn("第一次执行前", text)
            self.assertIn("先只读枚举本机可用", text)
            self.assertIn("环境名称", text)
            self.assertIn("解释器绝对路径", text)
            self.assertIn("用户选择", text)
            self.assertIn("不得自行选择", text)
            self.assertIn("选择前", text)
            self.assertIn("纯知识咨询不执行Python时不枚举也不询问环境", text)
            self.assertIn("不重复询问", text)
        self.assertIn("Host已明确注入`OPTIONHELPER_PYTHON`", skill)
        self.assertIn("Host已经设置`OPTIONHELPER_PYTHON`", readme)
        for text in (skill, readme):
            self.assertIn("依赖检查", text)
            self.assertIn("数据获取", text)
            self.assertIn("参数重算", text)
            self.assertIn("Reporter" if text is skill else "报告整理", text)
            self.assertIn('"$OPTIONHELPER_PYTHON" -m pip', text)
            self.assertIn("用户明确同意更换", text)
            self.assertIn("重新运行依赖检查" if text is readme else "从依赖检查重新开始", text)
        self.assertIn("不得在后续步骤改写为裸`python`、`python3`", skill)
        self.assertIn("不得在中途改用裸`python`、`python3`", readme)

    def test_missing_dependencies_stop_before_modules_and_require_user_choice(self) -> None:
        readme = (ROOT / "packaging" / "skill" / "SKILL_README.md").read_text(encoding="utf-8")
        skill = (ROOT / "SKILL.md").read_text(encoding="utf-8")

        for text in (skill, readme):
            self.assertIn("missing", text)
            self.assertIn("version_mismatch", text)
            self.assertIn("锁定版本", text)
            self.assertIn("当前版本", text)
            self.assertIn("改选另一个", text)
            self.assertIn("不得自动", text)
            self.assertIn('"ok": true', text)
        self.assertIn("安装失败时保留原始错误并停止", readme)

    def test_readme_is_setup_only_and_has_host_capability_matrix(self) -> None:
        readme = (ROOT / "packaging" / "skill" / "SKILL_README.md").read_text(encoding="utf-8")

        for heading in (
            "## 1.安装", "## 2.Python与依赖", "## 3.首次配置：只检查需要的能力", "## 4.Store",
            "## 5.项目级入口与能力范围", "## 6.Host能力矩阵",
        ):
            self.assertIn(heading, readme)
        self.assertIn("README只说明安装、依赖、凭据、Store和Host能力", readme)
        self.assertIn("运行行为以`SKILL.md`为准", readme)
        self.assertNotIn("## 7.模块JSON与报告流程", readme)

    def test_readme_preflight_is_route_aware_and_fail_closed(self) -> None:
        readme = (ROOT / "packaging" / "skill" / "SKILL_README.md").read_text(encoding="utf-8")

        self.assertIn("### 3.3按任务预检，不固定盘问", readme)
        self.assertIn("咨询、比较、结构推荐、Payoffer | 不检查iFind", readme)
        self.assertIn("先复用当前任务合格的DataAssetRef", readme)
        self.assertIn("当前数据服务尚未配置。请先配置iFind Refresh Token", readme)
        self.assertIn("不得先运行、失败后再追问", readme)
        self.assertIn("凭据无效或过期、网络不可用、Provider异常和数据权限不足", readme)

    def test_project_report_has_one_copyable_existing_entrypoint(self) -> None:
        readme = (ROOT / "packaging" / "skill" / "SKILL_README.md").read_text(encoding="utf-8")
        skill = (ROOT / "SKILL.md").read_text(encoding="utf-8")

        command = (
            '"$OPTIONHELPER_PYTHON" "$OPTIONHELPER_SKILL_ROOT/scripts/tool_entry.py" '
            "--project-request '我认为000300.SH未来会上涨，波动变大，给我推荐一个期权结构，并且生产报告'"
        )
        self.assertIn(command.replace("并且生产报告", "并且生成完整研究报告"), readme)
        self.assertIn("Recommender→DataFetcher→Payoffer→Pricer→Backtester→Reporter→Designer", readme)
        self.assertIn("不需要临时`.py`或手工HTML", readme)
        self.assertIn("当前对话Agent模式", readme)
        self.assertIn("批处理模式", readme)
        self.assertIn("--project-json", skill)
        self.assertIn("项目级独立CLI当前只支持单份HTML", readme)

    def test_project_store_defaults_are_initialized_without_questions(self) -> None:
        readme = (ROOT / "packaging" / "skill" / "SKILL_README.md").read_text(encoding="utf-8")
        skill = (ROOT / "SKILL.md").read_text(encoding="utf-8")

        for text in (readme, skill):
            self.assertIn("$PWD/result", text)
            self.assertIn("$PWD/data", text)
            self.assertIn("$PWD/.optionhelper/runtime", text)
        self.assertIn("Host首次运行时自动初始化", readme)
        self.assertIn("不要求用户逐次确认Store位置", readme)
        self.assertIn("环境变量覆盖", readme)
        self.assertIn("Host自动初始化安装目录之外的项目Store", skill)
        self.assertIn("不为默认Store新增提问", skill)

    def test_every_guide_keeps_internal_protocol_out_of_normal_dialogue(self) -> None:
        guides = {
            path.parent.name: path.read_text(encoding="utf-8")
            for path in (ROOT / "modules").glob("*/module-guide.md")
        }

        self.assertEqual(set(guides), {
            "datafetcher", "recommender", "payoffer", "pricer",
            "backtester", "reporter", "designer",
        })
        combined = "\n".join(guides.values())
        self.assertIn("不得要求普通用户提供Provider字段名、物理文件路径或Store位置", combined)
        self.assertIn("面向用户不展示`candidate_id`、JSON字段、Tool名", combined)
        self.assertIn("原始JSON仅在用户明确要求或系统集成时展示", combined)
        self.assertIn("不得为了确认默认格式或目录新增一轮对话", combined)
        self.assertIn("复用同一组已验证结果生成两份交付", combined)

    def test_global_context_matches_delivery_defaults(self) -> None:
        context = (ROOT / "CONTEXT.md").read_text(encoding="utf-8")

        self.assertIn("默认格式和版式不单独追问", context)
        self.assertIn("研究简报、完整研究报告或两份都要", context)
        self.assertIn("不展示内部字段、Tool名、运行标识、文件路径或环境信息", context)


if __name__ == "__main__":
    unittest.main()
