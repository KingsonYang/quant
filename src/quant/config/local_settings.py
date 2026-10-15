"""本机路径配置。

优先级：函数参数 > 环境变量 > config/local.yml。
不要把某台机器的绝对路径写回仓库。
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

import yaml


@dataclass(frozen=True)
class TdxQuantPaths:
    tqcenter_path: Path | None
    tdx_path: Path | None


def project_root() -> Path:
    here = Path(__file__).resolve()
    for parent in here.parents:
        if (parent / "pyproject.toml").is_file():
            return parent
    return here.parents[3]


def _text(value: object) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _env_path(name: str) -> str | None:
    return _text(os.environ.get(name))


def local_config_path() -> Path:
    override = _env_path("QUANT_LOCAL_CONFIG")
    if override:
        return Path(override)
    return project_root() / "config" / "local.yml"


def _file_section() -> dict[str, str]:
    path = local_config_path()
    if not path.is_file():
        return {}
    try:
        payload = yaml.safe_load(path.read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError) as exc:
        raise RuntimeError(f"无法读取本机配置 {path}: {exc}") from exc
    if payload is None:
        return {}
    if not isinstance(payload, dict):
        raise RuntimeError(f"本机配置必须是 YAML 映射: {path}")
    section = payload.get("tdxquant") or {}
    if not isinstance(section, dict):
        raise RuntimeError(f"tdxquant 配置必须是映射: {path}")
    values: dict[str, str] = {}
    for key in ("tqcenter_path", "tdx_path"):
        text = _text(section.get(key))
        if text:
            values[key] = text
    return values


def resolve_tdxquant_paths(
    *,
    tdx_path: Path | None = None,
    tqcenter_path: Path | None = None,
) -> TdxQuantPaths:
    """解析通达信路径。显式参数优先，其次环境变量，最后本机配置文件。"""
    file_values = _file_section()
    tqcenter = _text(tqcenter_path) or _env_path("QUANT_TQCENTER_PATH") or file_values.get("tqcenter_path")
    tdx = _text(tdx_path) or _env_path("QUANT_TDX_PATH") or file_values.get("tdx_path")
    return TdxQuantPaths(
        tqcenter_path=Path(tqcenter) if tqcenter else None,
        tdx_path=Path(tdx) if tdx else None,
    )


def missing_tqcenter_message() -> str:
    return (
        "未配置 tqcenter.py。"
        "请复制 config/local.example.yml 为 config/local.yml，"
        "填写通达信安装目录下的 PYPlugins/user/tqcenter.py；"
        "或设置环境变量 QUANT_TQCENTER_PATH。"
        "该文件必须位于通达信安装目录，因为它会按相对位置加载 TPythClient.dll。"
    )


def require_tqcenter_path(explicit: Path | None = None) -> Path:
    path = resolve_tdxquant_paths(tqcenter_path=explicit).tqcenter_path
    if path is None:
        raise FileNotFoundError(missing_tqcenter_message())
    return path
