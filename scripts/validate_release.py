"""发布前的轻量结构校验，不访问网络，也不会启动浏览器。"""

from __future__ import annotations

from pathlib import Path

import ast
import json
import re
import sys
import tomllib


REQUIRED_FILES = ("_manifest.json", "plugin.py", "LICENSE", "README.md", "CHANGELOG.md")
REQUIRED_MANIFEST_KEYS = {
    "manifest_version",
    "id",
    "version",
    "name",
    "description",
    "author",
    "license",
    "urls",
    "host_application",
    "sdk",
    "capabilities",
    "i18n",
}


def main() -> int:
    """检查仓库根目录、清单字段与默认安全开关。"""

    root = Path(__file__).resolve().parents[1]
    missing_files = [name for name in REQUIRED_FILES if not (root / name).is_file()]
    if missing_files:
        raise RuntimeError(f"缺少发布必需文件：{', '.join(missing_files)}")

    manifest = json.loads((root / "_manifest.json").read_text(encoding="utf-8"))
    missing_keys = REQUIRED_MANIFEST_KEYS - manifest.keys()
    if missing_keys:
        raise RuntimeError(f"manifest 缺少字段：{', '.join(sorted(missing_keys))}")
    if manifest["manifest_version"] != 2:
        raise RuntimeError("manifest_version 必须为 2")
    if manifest["license"] != "GPL-3.0-or-later":
        raise RuntimeError("当前发布候选版必须使用 GPL-3.0-or-later")
    if not isinstance(manifest["version"], str) or re.fullmatch(r"\d+\.\d+\.\d+", manifest["version"]) is None:
        raise RuntimeError("manifest.version 必须使用 x.y.z 语义版本号")

    config = tomllib.loads((root / "config.toml").read_text(encoding="utf-8"))
    if config["plugin"]["config_version"] != manifest["version"]:
        raise RuntimeError("config_version 必须与 manifest.version 保持一致")
    config_model_source = (root / "config_model.py").read_text(encoding="utf-8")
    config_model_version_match = re.search(
        r'config_version:\s*str\s*=\s*Field\(default="(\d+\.\d+\.\d+)"',
        config_model_source,
    )
    if config_model_version_match is None:
        raise RuntimeError("无法从 config_model.py 读取默认配置版本")
    if config_model_version_match.group(1) != manifest["version"]:
        raise RuntimeError("配置模型默认版本必须与 manifest.version 保持一致")
    if config["plugin"]["enabled"]:
        raise RuntimeError("发布默认配置的 plugin.enabled 必须为 false")
    if config["surf"]["enabled"]:
        raise RuntimeError("发布默认配置的 surf.enabled 必须为 false")
    if config["sharing"]["enabled"]:
        raise RuntimeError("发布默认配置的 sharing.enabled 必须为 false")
    if config["sharing"].get("stream_configs"):
        raise RuntimeError("发布默认配置不得预置群聊或私聊目标")
    if config["command_access"].get("allowed_targets"):
        raise RuntimeError("发布默认配置不得预置指令授权目标")
    if config["direct_text_model"].get("api_key") or config["direct_vision_model"].get("api_key"):
        raise RuntimeError("发布默认配置不得包含模型 API 密钥")
    if config["surf"]["active_hours"] != "09:00-23:00":
        raise RuntimeError("发布默认配置的 surf.active_hours 必须为 09:00-23:00")
    ast.parse((root / "plugin.py").read_text(encoding="utf-8"))
    print("发布结构校验通过")
    return 0


if __name__ == "__main__":
    sys.exit(main())
