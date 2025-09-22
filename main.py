import json
from typing import List, Dict, Any, Optional

from astrbot.api import logger, AstrBotConfig
from astrbot.api.event import filter, AstrMessageEvent
from astrbot.api.star import Context, Star, register
import astrbot.api.message_components as Comp

# 检查是否为 aiocqhttp 平台，因为合并转发是其特性
try:
    from astrbot.core.platform.sources.aiocqhttp.aiocqhttp_message_event import AiocqhttpMessageEvent
    IS_AIOCQHTTP = True
except ImportError:
    IS_AIOCQHTTP = False


@register("forward_reader", "EraAsh", "一个使用 LLM 分析合并转发消息内容的插件", "1.0.8", "https://github.com/EraAsh/astrbot_plugin_forward_reader")
class ForwardReader(Star):
    def __init__(self, context: Context, config: AstrBotConfig):
        super().__init__(context)
        self.config = config
        self._load_config()

    def _load_config(self):
        """加载配置"""
        self.enable_direct_analysis = self.config.get("enable_direct_analysis", False)
        self.enable_reply_analysis = self.config.get("enable_reply_analysis", True)
        self.system_prompt = self.config.get(
            "system_prompt",
            "你是一个专业的聊天记录分析助手。你的任务是根据用户提供的聊天记录（可能包含文字和图片）和用户的提问，进行总结和回答。如果用户没有明确提问，请对聊天记录进行一个全面的摘要。聊天记录的格式为 '发送者: 内容'。"
        )

    @filter.event_message_type(filter.EventMessageType.ALL)
    async def on_any_message(self, event: AstrMessageEvent, *args, **kwargs):
        """
        监听所有消息，智能判断是否需要分析合并转发。
        """
        if not IS_AIOCQHTTP or not isinstance(event, AiocqhttpMessageEvent):
            return

        forward_id: Optional[str] = None
        reply_seg: Optional[Comp.Reply] = None
        user_query: str = event.message_str.strip()

        # 遍历消息链寻找关键组件
        for seg in event.message_obj.message:
            if isinstance(seg, Comp.Forward):
                # 情况一：直接发送合并转发
                if self.enable_direct_analysis:
                    forward_id = seg.id
                    user_query = user_query if user_query else "请总结一下这个聊天记录"
                    break  # 找到转发就不用继续了
            elif isinstance(seg, Comp.Reply):
                reply_seg = seg

        # 情况二：回复一条合并转发消息
        if self.enable_reply_analysis and not forward_id and reply_seg:
            replied_msg_id = reply_seg.id
            try:
                # 调用API获取被回复的消息的详情
                client = event.bot
                original_msg = await client.api.call_action('get_msg', message_id=replied_msg_id)
                if original_msg and 'message' in original_msg:
                    for segment in original_msg['message']:
                        if segment.get("type") == "forward":
                            forward_id = segment.get("data", {}).get("id")
                            break
            except Exception as e:
                logger.warning(f"获取被回复消息详情失败: {e}")

        if forward_id:
            # 找到了需要分析的合并转发消息，开始处理
            try:
                # 发送一个等待消息，改善用户体验
                yield event.chain_result([Comp.Reply(id=event.message_obj.message_id), Comp.Plain("正在分析聊天记录，请稍候...")])
                
                async for result in self._process_forward_message(event, forward_id, user_query):
                    yield result
                event.stop_event() # 已经处理，阻止其他插件响应

            except Exception as e:
                logger.error(f"分析转发消息失败: {e}")
                yield event.plain_result(f"分析失败: {e}")

    async def _process_forward_message(self, event: AiocqhttpMessageEvent, forward_id: str, user_query: str):
        """
        处理合并转发消息的核心逻辑
        """
        client = event.bot
        forward_data = await client.api.call_action('get_forward_msg', id=forward_id)

        if not forward_data or "messages" not in forward_data:
            raise ValueError("获取合并转发内容失败或内容为空。")

        extracted_texts = []
        image_urls = []

        for message_node in forward_data["messages"]:
            sender_name = message_node.get("sender", {}).get("nickname", "未知用户")
            # --- 最终修复：使用正确的 'message' 键 ---
            content_chain = message_node.get("message", [])

            node_text_parts = []
            if isinstance(content_chain, str):
                node_text_parts.append(content_chain)
            elif isinstance(content_chain, list):
                for segment in content_chain:
                    if isinstance(segment, dict):
                        seg_type = segment.get("type")
                        if seg_type == "text":
                            text = segment.get("data", {}).get("text", "")
                            if text:
                                node_text_parts.append(text)
                        elif seg_type == "image":
                            url = segment.get("data", {}).get("url")
                            if url:
                                image_urls.append(url)
                                node_text_parts.append("[图片]")

            full_node_text = "".join(node_text_parts)
            if full_node_text:
                extracted_texts.append(f"{sender_name}: {full_node_text}")

        if not extracted_texts:
            yield event.plain_result("无法从合并转发消息中提取到任何有效文本内容。")
            return

        # 构建更自然的 Prompt
        final_prompt = f"这是用户的问题：'{user_query}'\n\n" \
                       "请根据以下聊天记录内容来回答用户的问题。聊天记录如下：\n" + \
                       "\n".join(extracted_texts)

        logger.info(f"ForwardReader: 准备向LLM发送请求，Prompt长度: {len(final_prompt)}, 图片数量: {len(image_urls)}")

        # 请求 LLM 进行分析，并直接将请求对象 yield 出去，由框架处理
        yield event.request_llm(
            prompt=final_prompt,
            image_urls=image_urls,
            system_prompt=self.system_prompt
        )

    async def terminate(self):
        pass
