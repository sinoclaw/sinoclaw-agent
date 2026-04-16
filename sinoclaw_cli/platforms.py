"""
Shared platform registry for Sinoclaw Agent.

Single source of truth for platform metadata consumed by both
skills_config (label display) and tools_config (default toolset
resolution).  Import ``PLATFORMS`` from here instead of maintaining
duplicate dicts in each module.
"""

from collections import OrderedDict
from typing import NamedTuple


class PlatformInfo(NamedTuple):
    """Metadata for a single platform entry."""
    label: str
    default_toolset: str


# Ordered so that TUI menus are deterministic.
PLATFORMS: OrderedDict[str, PlatformInfo] = OrderedDict([
    ("cli",            PlatformInfo(label="🖥️  CLI",            default_toolset="sinoclaw-cli")),
    ("telegram",       PlatformInfo(label="📱 Telegram",        default_toolset="sinoclaw-telegram")),
    ("discord",        PlatformInfo(label="💬 Discord",         default_toolset="sinoclaw-discord")),
    ("slack",          PlatformInfo(label="💼 Slack",           default_toolset="sinoclaw-slack")),
    ("whatsapp",       PlatformInfo(label="📱 WhatsApp",        default_toolset="sinoclaw-whatsapp")),
    ("signal",         PlatformInfo(label="📡 Signal",          default_toolset="sinoclaw-signal")),
    ("bluebubbles",    PlatformInfo(label="💙 BlueBubbles",     default_toolset="sinoclaw-bluebubbles")),
    ("email",          PlatformInfo(label="📧 Email",           default_toolset="sinoclaw-email")),
    ("homeassistant",  PlatformInfo(label="🏠 Home Assistant",  default_toolset="sinoclaw-homeassistant")),
    ("mattermost",     PlatformInfo(label="💬 Mattermost",      default_toolset="sinoclaw-mattermost")),
    ("matrix",         PlatformInfo(label="💬 Matrix",          default_toolset="sinoclaw-matrix")),
    ("dingtalk",       PlatformInfo(label="💬 DingTalk",        default_toolset="sinoclaw-dingtalk")),
    ("feishu",         PlatformInfo(label="🪽 Feishu",          default_toolset="sinoclaw-feishu")),
    ("wecom",          PlatformInfo(label="💬 WeCom",           default_toolset="sinoclaw-wecom")),
    ("wecom_callback", PlatformInfo(label="💬 WeCom Callback",  default_toolset="sinoclaw-wecom-callback")),
    ("weixin",         PlatformInfo(label="💬 Weixin",          default_toolset="sinoclaw-weixin")),
    ("qqbot",          PlatformInfo(label="💬 QQBot",           default_toolset="sinoclaw-qqbot")),
    ("webhook",        PlatformInfo(label="🔗 Webhook",         default_toolset="sinoclaw-webhook")),
    ("api_server",     PlatformInfo(label="🌐 API Server",      default_toolset="sinoclaw-api-server")),
])


def platform_label(key: str, default: str = "") -> str:
    """Return the display label for a platform key, or *default*."""
    info = PLATFORMS.get(key)
    return info.label if info is not None else default
