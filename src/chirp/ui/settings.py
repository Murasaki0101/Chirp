"""App settings — read/write the .env config + a Settings dialog.

Gives the user an in-app switch between:
  - Decision engine: local Laya (offline) vs cloud Jev (needs API key)
  - LLM for deep interpretation / reply generation (user-provided endpoint)
  - Their own DingTalk senderId, poll interval, etc.

All values persist to `.env` (comments and unmanaged lines are preserved).
"""
from __future__ import annotations

import logging
import os
from pathlib import Path

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QButtonGroup, QDialog, QFormLayout, QGroupBox, QHBoxLayout, QLabel,
    QLineEdit, QMessageBox, QPushButton, QRadioButton, QSpinBox, QVBoxLayout,
    QWidget,
)

log = logging.getLogger(__name__)

from ..infrastructure.jev_client import get_env_path  # noqa: E402  统一配置路径（打包后=~/.dingtalk-radar/.env，可写）

ENV_PATH = get_env_path()

# Keys the Settings dialog manages (order = display order within sections)
ENGINE_KEY = "RADAR_ENGINE"
JEV_KEY = "TYPESAFE_API_KEY"
LLM_BASE = "RADAR_LLM_BASE_URL"
LLM_KEY = "RADAR_LLM_API_KEY"
LLM_MODEL = "RADAR_LLM_MODEL"
SELF_ID = "RADAR_SELF_ID"
POLL = "RADAR_POLL_INTERVAL"
LAYA_MODEL = "RADAR_LAYA_MODEL"


# ---------------------------------------------------------------------------
# .env read / write (preserves comments + unmanaged lines)
# ---------------------------------------------------------------------------

def read_env(path: Path = ENV_PATH) -> dict[str, str]:
    env: dict[str, str] = {}
    if not path.exists():
        return env
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        env[k.strip()] = v.strip()
    return env


def write_env(updates: dict[str, str], path: Path = ENV_PATH) -> None:
    """Update specific keys in .env, preserving everything else."""
    lines: list[str] = []
    if path.exists():
        lines = path.read_text(encoding="utf-8").splitlines()

    seen: set[str] = set()
    out: list[str] = []
    for line in lines:
        stripped = line.strip()
        if stripped and not stripped.startswith("#") and "=" in stripped:
            k = stripped.split("=", 1)[0].strip()
            if k in updates:
                out.append(f"{k}={updates[k]}")
                seen.add(k)
                continue
        out.append(line)

    # append any managed keys not already present
    for k, v in updates.items():
        if k not in seen:
            out.append(f"{k}={v}")

    # Keep credentials readable only by the current user.  This applies to
    # both the source checkout's .env and the frozen app's user config.
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    # Do not chmod the whole source checkout when the development .env lives
    # beside the code.  The frozen app's ~/.dingtalk-radar directory is already
    # tightened by get_env_path().
    if path.parent.name == ".dingtalk-radar":
        try:
            os.chmod(path.parent, 0o700)
        except OSError:
            pass
    path.write_text("\n".join(out) + "\n", encoding="utf-8")
    try:
        os.chmod(path, 0o600)
    except OSError:
        pass
    # reflect into the current process env too
    for k, v in updates.items():
        if v:
            os.environ[k] = v
        else:
            os.environ.pop(k, None)


# ---------------------------------------------------------------------------
# Settings dialog
# ---------------------------------------------------------------------------

class SettingsDialog(QDialog):
    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setWindowTitle("吱一声 · 设置")
        self.setMinimumWidth(460)
        self.setModal(True)
        self._env = read_env()
        self._build()

    # ------------------------------------------------------------------

    def _build(self) -> None:
        def configured(key: str, default: str = "") -> str:
            # A value supplied by the parent process is valid configuration too.
            # Showing it here lets the user persist it through the settings
            # panel instead of accidentally overwriting it with an empty value.
            return self._env.get(key, os.environ.get(key, default))

        root = QVBoxLayout(self)
        root.setContentsMargins(16, 16, 16, 16)
        root.setSpacing(12)

        # --- Engine selection ---
        eng_box = QGroupBox("决策引擎（意图/情绪/PUA/紧急度判断）")
        eng_lay = QVBoxLayout(eng_box)
        self.rb_laya = QRadioButton("🖥 本地 Laya（开源、离线、免费，首次需下载 ~650M 模型）")
        self.rb_jev = QRadioButton("☁️ 云端 Jev（TypeSafe，需 API Key，更快更准）")
        self.rb_mock = QRadioButton("🧪 Mock（测试用，不调任何模型）")
        self.engine_group = QButtonGroup(self)
        for rb in (self.rb_laya, self.rb_jev, self.rb_mock):
            self.engine_group.addButton(rb)
            eng_lay.addWidget(rb)
        cur = configured(ENGINE_KEY).lower()
        if cur in ("jev", "cloud"):
            self.rb_jev.setChecked(True)
        elif cur == "mock":
            self.rb_mock.setChecked(True)
        else:
            self.rb_laya.setChecked(True)   # default to local
        root.addWidget(eng_box)

        # --- Jev key ---
        jev_box = QGroupBox("云端 Jev API Key")
        jev_form = QFormLayout(jev_box)
        self.jev_key = QLineEdit(configured(JEV_KEY))
        self.jev_key.setEchoMode(QLineEdit.EchoMode.Password)
        self.jev_key.setPlaceholderText("apikey_...（选本地 Laya 可不填）")
        jev_form.addRow("Key:", self.jev_key)
        root.addWidget(jev_box)

        # --- LLM for interpretation ---
        llm_box = QGroupBox("大语言模型（详细解读 / 回复生成，可选）")
        llm_form = QFormLayout(llm_box)
        self.llm_base = QLineEdit(configured(LLM_BASE))
        self.llm_base.setPlaceholderText("https://your-endpoint/v1（OpenAI 兼容）")
        self.llm_key = QLineEdit(configured(LLM_KEY))
        self.llm_key.setEchoMode(QLineEdit.EchoMode.Password)
        self.llm_model = QLineEdit(configured(LLM_MODEL))
        self.llm_model.setPlaceholderText("例如 gpt-4o-mini / qwen-turbo")
        llm_form.addRow("Base URL:", self.llm_base)
        llm_form.addRow("API Key:", self.llm_key)
        llm_form.addRow("Model:", self.llm_model)
        note = QLabel("不填则详细解读用 Jev/Laya 模板模式（已可用，只是没 LLM 自然）。")
        note.setStyleSheet("color:#888; font-size:11px;")
        note.setWordWrap(True)
        llm_form.addRow(note)
        root.addWidget(llm_box)

        # --- Misc ---
        misc_box = QGroupBox("其他")
        misc_form = QFormLayout(misc_box)
        self.dws_bin = QLineEdit(configured("DWS_BIN"))
        self.dws_bin.setPlaceholderText("官方 dws 路径，留空自动从 PATH 查找")
        misc_form.addRow("钉钉 CLI:", self.dws_bin)
        self.self_id = QLineEdit(configured(SELF_ID))
        self.self_id.setPlaceholderText("你的钉钉 senderId，用于过滤自己发的消息")
        misc_form.addRow("我的 senderId:", self.self_id)
        self.poll = QSpinBox()
        self.poll.setRange(5, 600)
        self.poll.setSuffix(" 秒")
        try:
            self.poll.setValue(int(configured(POLL, "30")))
        except ValueError:
            self.poll.setValue(30)
        misc_form.addRow("刷新间隔:", self.poll)
        root.addWidget(misc_box)

        # --- Buttons ---
        btns = QHBoxLayout()
        btns.addStretch(1)
        save = QPushButton("保存")
        save.setDefault(True)
        save.clicked.connect(self._save)
        cancel = QPushButton("取消")
        cancel.clicked.connect(self.reject)
        btns.addWidget(save)
        btns.addWidget(cancel)
        root.addLayout(btns)

    # ------------------------------------------------------------------

    def _save(self) -> None:
        if self.rb_jev.isChecked() and not self.jev_key.text().strip():
            QMessageBox.warning(self, "缺少 Key", "选了云端 Jev 就需要填 API Key。")
            return
        engine = "laya" if self.rb_laya.isChecked() else \
                 ("jev" if self.rb_jev.isChecked() else "mock")
        updates = {
            ENGINE_KEY: engine,
            JEV_KEY: self.jev_key.text().strip(),
            LLM_BASE: self.llm_base.text().strip(),
            LLM_KEY: self.llm_key.text().strip(),
            LLM_MODEL: self.llm_model.text().strip(),
            SELF_ID: self.self_id.text().strip(),
            POLL: str(self.poll.value()),
            "DWS_BIN": self.dws_bin.text().strip(),
        }
        write_env(updates)
        log.info("settings saved: engine=%s", engine)
        self.accept()

    @staticmethod
    def selected_engine(dialog: "SettingsDialog") -> str:
        if dialog.rb_jev.isChecked():
            return "jev"
        if dialog.rb_mock.isChecked():
            return "mock"
        return "laya"
