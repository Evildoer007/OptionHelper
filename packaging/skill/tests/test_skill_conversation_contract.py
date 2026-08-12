"""模型中立的Skill对话与安装说明契约。"""

from __future__ import annotations

from pathlib import Path
import unittest


ROOT = Path(__file__).resolve().parents[3]


class SkillConversationContractTests(unittest.TestCase):
    def test_skill_forbids_ad_hoc_source_and_report_bypasses(self) -> None:
        skill = (ROOT / "SKILL.md").read_text(encoding="utf-8")

        self.assertIn("普通研究、咨询、计算和报告任务不得新建或编辑宿主项目中的`.py`、HTML或其他源码", skill)
        self.assertIn("只能调用Skill已经提供的现成模块", skill)
        self.assertIn("任何报告都必须先由Reporter整理已验证事实，再由Designer生成HTML或PDF", skill)
        self.assertIn("Reporter或Designer不可用时，明确说明报告能力不可用", skill)
        self.assertIn("只有用户明确要求开发或修改代码时", skill)

    def test_delivery_language_is_professional_and_hides_internal_rules(self) -> None:
        skill = (ROOT / "SKILL.md").read_text(encoding="utf-8")

        self.assertIn("对用户只使用“研究简报”和“完整研究报告”", skill)
        self.assertIn("研究简报 | 1.推荐结构与标的；2.推荐依据", skill)
        self.assertIn("完整研究报告 | 1.核心结论；2.结构推荐；3.合同参数；4.收益结构；5.估值定价；6.历史回测；7.风险提示", skill)
        self.assertIn("不要向普通用户解释`card`、`report`、版式枚举或内部请求字段", skill)
        self.assertNotIn("（不含损益图）", skill)
        self.assertNotIn("你选哪个", skill)

    def test_existing_facts_and_delivery_choice_are_not_asked_again(self) -> None:
        skill = (ROOT / "SKILL.md").read_text(encoding="utf-8")

        self.assertIn("不得因为进入新模块、确认候选或生成交付物而重复询问", skill)
        self.assertIn("用户已经提出“报告”“简报”“详细报告”“两份都要”", skill)
        self.assertIn("用户要求两份时", skill)
        self.assertIn("上游分析只运行一次", skill)

    def test_report_is_never_upsold_and_defaults_are_automatic(self) -> None:
        skill = (ROOT / "SKILL.md").read_text(encoding="utf-8")

        self.assertIn("用户未提出报告时直接完成分析", skill)
        self.assertIn("不主动询问是否需要报告", skill)
        self.assertIn("用户提出报告但未指定格式时直接生成HTML", skill)
        self.assertIn("完整研究报告未指定版式时使用连续版", skill)
        self.assertNotIn("用户尚未表达交付需求时，只问一次", skill)
        self.assertNotIn("只问一次是否需要研究简报或完整研究报告", skill)

    def test_skill_preflights_only_capabilities_required_by_the_route(self) -> None:
        skill = (ROOT / "SKILL.md").read_text(encoding="utf-8")

        self.assertIn("## 能力预检", skill)
        self.assertIn("咨询、比较、条款解释、只做结构推荐", skill)
        self.assertIn("数据凭据；第二套模型配置", skill)
        self.assertIn("当前任务已有满足口径的DataAssetRef", skill)
        self.assertIn("当前数据服务尚未配置。请先配置iFind Refresh Token", skill)
        self.assertIn("禁止要求Access Token", skill)
        self.assertIn("当前对话Agent模式中，当前对话模型已经满足模型能力", skill)
        self.assertIn("不检查`OPTIONHELPER_MODEL_*`", skill)

    def test_skill_never_guesses_the_python_environment(self) -> None:
        skill = (ROOT / "SKILL.md").read_text(encoding="utf-8")

        self.assertIn("先只读枚举本机可用的Conda环境", skill)
        self.assertIn("向用户展示", skill)
        self.assertIn("不得自行选择第一个、当前、base", skill)
        self.assertIn("用户明确选择前不得运行候选解释器", skill)
        self.assertIn("纯知识咨询不执行Python时不枚举也不询问环境", skill)
        self.assertNotIn("检查通过后继续，不询问环境名称", skill)

    def test_skill_never_repairs_dependencies_without_permission(self) -> None:
        skill = (ROOT / "SKILL.md").read_text(encoding="utf-8")

        self.assertIn("库缺失`missing`或版本不符`version_mismatch`", skill)
        self.assertIn("立即停止模块执行", skill)
        self.assertIn("不得自动运行`pip install`", skill)
        self.assertIn("只有用户明确授权安装后", skill)

    def test_routes_have_one_authority_and_do_not_overlap(self) -> None:
        skill = (ROOT / "SKILL.md").read_text(encoding="utf-8")

        self.assertIn("本文件决定用户意图路由", skill)
        self.assertIn("## 三种用户工作流", skill)
        self.assertIn("### 强制执行门禁", skill)
        self.assertIn("进入Reporter后，只有Designer可以生成HTML或PDF", skill)
        self.assertIn("用户已经指定产品时，只验证该产品", skill)
        self.assertIn("指定模块与参数重算", skill)
        self.assertIn("Recommender→必要的数据与计算→Reporter→Designer", skill)
        self.assertIn("交付形式与分析覆盖是两个概念", skill)
        self.assertIn("项目级独立CLI只支持单份HTML", skill)

    def test_formal_delivery_workflow_and_report_schema_are_non_negotiable(self) -> None:
        skill = (ROOT / "SKILL.md").read_text(encoding="utf-8")
        recommender = (ROOT / "modules" / "recommender" / "module-guide.md").read_text(encoding="utf-8")
        reporter = (ROOT / "modules" / "reporter" / "module-guide.md").read_text(encoding="utf-8")
        headings = "核心结论、结构推荐、合同参数、收益结构、估值定价、历史回测、风险提示"

        for document in (skill, recommender, reporter):
            self.assertIn(headings, document)
        self.assertIn("必须完整通过`Recommender→DataFetcher（需要新数据时）→Payoffer→Pricer→Backtester→Reporter→Designer`", skill)
        self.assertIn("模块返回的JSON只在模块之间传递", skill)
        self.assertIn("不经Reporter→Designer自行输出文件", skill)
        self.assertIn("不提供目录版", skill)
        self.assertNotIn("市场观点与适用范围", skill)
        self.assertNotIn("推荐结构与关键条款", skill)

    def test_recommendation_is_client_side_and_terms_are_confirmed_before_execution(self) -> None:
        skill = (ROOT / "SKILL.md").read_text(encoding="utf-8")
        recommender = (ROOT / "modules" / "recommender" / "module-guide.md").read_text(encoding="utf-8")

        for document in (skill, recommender):
            self.assertIn("客户", document)
            self.assertIn("发行便利", document)
            self.assertIn("销售偏好", document)
            self.assertIn("拟采用合同条款", document)
            self.assertIn("名义本金", document)
            self.assertIn("行权价", document)
            self.assertIn("障碍", document)
            self.assertIn("观察频率", document)
            self.assertIn("结算方式", document)
            self.assertIn("不代表合同条款", document)
        self.assertIn("是否按此继续", skill)
        self.assertIn("直接给出新值本身就是确认", recommender)
        self.assertNotIn("报告请求同时构成对资料完整主候选的执行授权", skill)
        self.assertNotIn("正式交付可以直接绑定该主候选", recommender)

    def test_readme_is_host_neutral_and_declares_runtime(self) -> None:
        readme = (ROOT / "packaging" / "skill" / "SKILL_README.md").read_text(encoding="utf-8")

        self.assertIn("Agent Host的项目级Skill目录", readme)
        self.assertIn("Python3.11或更高版本", readme)
        self.assertIn("scripts/requirements.lock", readme)
        for dependency in ("numpy", "pandas", "scipy", "numba", "requests", "jsonschema", "PyYAML", "reportlab"):
            self.assertIn(dependency, readme)
        self.assertIn("当前Agent本身就是对话模型", readme)
        self.assertIn("普通研究对话默认先给专业结论", readme)

    def test_all_module_guides_share_the_same_interaction_boundary(self) -> None:
        guides = {
            path.parent.name: path.read_text(encoding="utf-8")
            for path in (ROOT / "modules").glob("*/module-guide.md")
        }

        self.assertEqual(set(guides), {
            "datafetcher", "recommender", "payoffer", "pricer",
            "backtester", "reporter", "designer",
        })
        self.assertIn("不得要求用户同时维护两种Token", guides["datafetcher"])
        self.assertIn("当前数据服务尚未配置。请先配置iFind Refresh Token", guides["datafetcher"])
        self.assertIn("一次合并问题", guides["recommender"])
        self.assertIn("只做结构推荐时不得为了未来可能计算而检查iFind", guides["recommender"])
        self.assertIn("不默认载入任何特定产品", guides["payoffer"])
        self.assertIn("不需要行情数据或iFind凭据", guides["payoffer"])
        self.assertIn("不为该默认值单独追问", guides["pricer"])
        self.assertIn("引用合格时不重新检查数据凭据", guides["pricer"])
        self.assertIn("多个关键缺口一次列出", guides["backtester"])
        self.assertIn("引用合格时不重新检查数据凭据", guides["backtester"])
        self.assertIn("不得为了确认默认格式或目录新增一轮对话", guides["reporter"])
        self.assertIn("不重新检查iFind、不重新取数或重算", guides["reporter"])
        self.assertIn("Designer不向用户追问", guides["designer"])
        self.assertIn("Designer不检查模型配置或数据凭据", guides["designer"])
        self.assertIn("ReportLab", guides["designer"])
        self.assertNotIn("WeasyPrint", guides["designer"])

    def test_every_module_has_human_friendly_lifecycle_copy(self) -> None:
        guides = {
            path.parent.name: path.read_text(encoding="utf-8")
            for path in (ROOT / "modules").glob("*/module-guide.md")
        }

        for module, guide in guides.items():
            with self.subTest(module=module):
                self.assertIn("## 用户可见进度", guide)
                for phase in ("开始：", "进度：", "完成：", "失败："):
                    self.assertIn(phase, guide)
                self.assertIn("进度最多更新1至2次", guide)
                self.assertIn("不要求用户回复", guide)
                self.assertIn("不包含Tool名、JSON字段、运行标识、环境名称或物理路径", guide)

        for module in ("datafetcher", "recommender", "payoffer", "pricer", "backtester"):
            with self.subTest(progress_owner=module):
                self.assertIn("由顶层工作流统一发进度", guides[module])
        self.assertIn("Reporter作为报告阶段的进度所有者", guides["reporter"])
        self.assertIn("由Reporter统一发报告阶段进度", guides["designer"])

    def test_global_context_separates_user_language_from_json_protocol(self) -> None:
        context = (ROOT / "CONTEXT.md").read_text(encoding="utf-8")

        self.assertIn("普通用户界面默认给专业结论、依据和限制", context)
        self.assertIn("多个关键缺口一次合并确认", context)
        self.assertIn("不得重复确认交付类型", context)
        self.assertIn("能力预检按本次请求最小化执行", context)
        self.assertIn("当前对话Agent模式下，当前对话模型就是模型能力", context)
        self.assertIn("交付形式与分析覆盖分开记录", context)


if __name__ == "__main__":
    unittest.main()
