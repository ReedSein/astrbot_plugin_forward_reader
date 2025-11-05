import json
from pathlib import Path
from typing import Dict, Any, Optional

from astrbot.api import logger, AstrBotConfig
from astrbot.api.star import Context, Star, register, StarTools
from astrbot.api.event import filter, AstrMessageEvent
import astrbot.api.message_components as Comp

# 默认好感度数据结构
DEFAULT_FAVOUR_DATA = {
    "users": {}  # {user_id: favour_value}
}

class FavourManager:
    """
    好感度管理类，负责数据的加载、保存和操作。
    """
    def __init__(self, context: Context):
        self.context = context
        self.data_path: Path = StarTools.get_data_dir(context) / "favour_data.json"
        self.data: Dict[str, Any] = DEFAULT_FAVOUR_DATA
        self._load_data()

    def _load_data(self):
        """从文件加载好感度数据"""
        if self.data_path.exists():
            try:
                with open(self.data_path, 'r', encoding='utf-8') as f:
                    self.data = json.load(f)
                logger.info(f"好感度数据从 {self.data_path} 加载成功。")
            except Exception as e:
                logger.error(f"加载好感度数据失败: {e}")
                self.data = DEFAULT_FAVOUR_DATA
        else:
            logger.info(f"好感度数据文件 {self.data_path} 不存在，使用默认数据。")
            self.data_path.parent.mkdir(parents=True, exist_ok=True)
            self._save_data()

    def _save_data(self):
        """保存好感度数据到文件"""
        try:
            with open(self.data_path, 'w', encoding='utf-8') as f:
                json.dump(self.data, f, ensure_ascii=False, indent=4)
            logger.debug(f"好感度数据保存到 {self.data_path} 成功。")
        except Exception as e:
            logger.error(f"保存好感度数据失败: {e}")

    def get_favour(self, user_id: str) -> int:
        """获取指定用户的好感度值"""
        return self.data["users"].get(user_id, 0)

    def set_favour(self, user_id: str, value: int):
        """设置指定用户的好感度值"""
        self.data["users"][user_id] = value
        self._save_data()

    def reset_negative_favour(self) -> int:
        """
        重置所有用户的负面好感度（即好感度 < 0 的用户，将其好感度设置为 0）。
        返回被重置的用户数量。
        """
        reset_count = 0
        users_to_reset = []
        for user_id, favour in self.data["users"].items():
            if favour < 0:
                users_to_reset.append(user_id)
        
        for user_id in users_to_reset:
            self.data["users"][user_id] = 0
            reset_count += 1
            
        if reset_count > 0:
            self._save_data()
            
        return reset_count

@register("favour_command", "EraAsh", "好感度命令扩展", "1.0.0", "https://github.com/EraAsh/astrbot_plugin_forward_reader")
class FavourCommand(Star):
    """
    处理好感度相关命令的插件。
    """
    def __init__(self, context: Context, config: AstrBotConfig):
        super().__init__(context)
        self.favour_manager = FavourManager(context)
        self.config = config
        self.admin_qq = self.config.get("admin_qq", None) # 假设配置中可以获取管理员QQ

    @filter.event_message_type(filter.EventMessageType.ALL)
    @filter.command("/重置负面")
    async def reset_negative_favour_command(self, event: AstrMessageEvent, *args, **kwargs):
        """
        处理 /重置负面 命令，重置所有用户的负面好感度。
        """
        # 1. 权限检查：确保只有管理员可以执行此命令
        sender_id = str(event.sender.id)
        if self.admin_qq and sender_id != str(self.admin_qq):
            logger.warning(f"非管理员用户 {sender_id} 尝试执行 /重置负面 命令。")
            yield event.plain_result("权限不足，只有管理员可以执行此命令。")
            return

        # 2. 执行重置操作
        reset_count = self.favour_manager.reset_negative_favour()

        # 3. 构造回复消息
        if reset_count > 0:
            reply_text = f"成功重置了 {reset_count} 个用户的负面好感度，他们的好感度已设置为 0。"
        else:
            reply_text = "没有发现负面好感度的用户，无需重置。"

        logger.info(f"管理员 {sender_id} 执行 /重置负面 命令，结果: {reply_text}")
        
        # 4. 发送回复
        yield event.plain_result(reply_text)
        event.stop_event()

    async def terminate(self):
        pass

# --------------------------------------------------------------------------------
# 为了让插件生效，我们需要在 main.py 中导入并注册 FavourCommand 类。
# 但是为了不破坏原有的 forward_reader 逻辑，我们直接在 main.py 中添加 FavourCommand 的注册。
# --------------------------------------------------------------------------------
