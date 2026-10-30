"""应用名称与版本号（引擎层与界面层共用，避免多处硬编码）。"""

APP_NAME = "A Minecraft Launcher"
APP_VERSION = "1.0.1"


def user_agent() -> str:
    return f"{APP_NAME}/{APP_VERSION}"
