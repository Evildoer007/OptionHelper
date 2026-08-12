from pathlib import Path
import unittest


ROOT = Path(__file__).resolve().parents[3]


class FinalWorkspaceRegressionTests(unittest.TestCase):
    def test_desk_keeps_original_light_palette(self):
        css = (ROOT / "products/app/frontend/shared/refinement.css").read_text(encoding="utf-8")
        self.assertIn('[data-mode="desk"] .workspace-rail', css)
        self.assertIn("background: #f6f7f6", css)
        self.assertIn("border-right-color: #dfe2df", css)

    def test_saved_credentials_remain_visibly_masked(self):
        js = (ROOT / "products/app/frontend/settings/settings.js").read_text(encoding="utf-8")
        self.assertIn("••••••••••••（已保存）", js)
        self.assertIn("data.credential_configured", js)

    def test_default_app_uses_prompt_free_local_credential_store(self):
        server = (ROOT / "products/app/backend/app_server.py").read_text(encoding="utf-8")
        settings = (ROOT / "products/app/frontend/settings/settings.js").read_text(encoding="utf-8")
        self.assertIn('"local-secret" if self.secret_provider.supports("local-secret")', server)
        self.assertIn('LocalSecretStore(app_data_root / "credentials")', server)
        self.assertNotIn("已保存至本机钥匙串", settings)
        self.assertIn("modelCredentialOrigin", settings)
        self.assertIn("更换模型服务后", settings)

    def test_choice_menu_can_flip_above_composer(self):
        js = (ROOT / "products/app/frontend/shared/app.js").read_text(encoding="utf-8")
        css = (ROOT / "products/app/frontend/shared/styles.css").read_text(encoding="utf-8")
        self.assertIn("positionChoiceMenu", js)
        self.assertIn('choice.dataset.placement = roomBelow < desiredHeight', js)
        self.assertIn('.choice[data-placement="top"] .choice-menu', css)

    def test_conversation_states_do_not_mislabel_model_or_move_composer(self):
        optchat = (ROOT / "products/app/frontend/optchat/optchat.js").read_text(encoding="utf-8")
        styles = (ROOT / "products/app/frontend/shared/styles.css").read_text(encoding="utf-8")
        self.assertNotIn('if (response.status !== "completed") message(status, `模型暂不可用', optchat)
        self.assertIn('needs_input: "等待补充信息后继续处理。"', optchat)
        self.assertIn('if (state === "unavailable")', optchat)
        self.assertIn('[data-mode="chat"] #workspace-status', styles)
        self.assertIn('bottom: calc(100% + 8px)', styles)
        self.assertIn('position: absolute', styles)


if __name__ == "__main__":
    unittest.main()
