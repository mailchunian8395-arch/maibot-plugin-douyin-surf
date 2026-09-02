"""将运行副本的公开源码同步到本机独立的发布工作副本。"""

from __future__ import annotations

from argparse import ArgumentParser
from pathlib import Path
from shutil import copy2, copytree


EXCLUDED_DIRECTORY_NAMES = {
    ".git",
    ".pytest_cache",
    "__pycache__",
    "browser-profile",
    "config_back",
    "data",
    "logs",
    "video-duration-probe-cache",
    "video-share-cache",
}
EXCLUDED_FILE_NAMES = {"config.toml", "V2_HANDOFF.md"}


def _ignore(source: str, names: list[str]) -> set[str]:
    """排除运行数据、本机配置和 Git 元数据，只复制可发布源码。"""

    del source
    return {
        name
        for name in names
        if name in EXCLUDED_DIRECTORY_NAMES or name in EXCLUDED_FILE_NAMES
    }


def sync_public_source(source: Path, target: Path) -> list[Path]:
    """同步公开文件，保留目标目录的默认配置与独立 Git 仓库。"""

    if not (target / ".git").is_dir():
        raise RuntimeError(f"发布工作副本不是 Git 仓库：{target}")
    copied: list[Path] = []
    for item in source.iterdir():
        if item.name in EXCLUDED_DIRECTORY_NAMES or item.name in EXCLUDED_FILE_NAMES:
            continue
        destination = target / item.name
        if item.is_dir():
            copytree(item, destination, ignore=_ignore, dirs_exist_ok=True)
            copied.append(destination)
        elif item.is_file():
            copy2(item, destination)
            copied.append(destination)
    return copied


def main() -> int:
    source = Path(__file__).resolve().parents[1]
    default_target = source.parents[2] / "maibot-plugin-douyin-surf-publish"
    parser = ArgumentParser(description="同步抖音冲浪插件的公开源码到发布工作副本")
    parser.add_argument("--target", type=Path, default=default_target, help="发布工作副本目录")
    args = parser.parse_args()

    copied = sync_public_source(source, args.target.resolve())
    print(f"已同步 {len(copied)} 个公开项目到 {args.target.resolve()}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
