import json
from typing import List, Dict, Any, Optional

from astrbot.api import logger, AstrBotConfig
from astrbot.api.event import filter, AstrMessageEvent, MessageChain
from astrbot.api.star import Context, Star, register
import astrbot.api.message_components as Comp

# 检查是否为 aiocqhttp 平台
try:
    from astrbot.core.platform.sources.aiocqhttp.aiocqhttp_message_event import AiocqhttpMessageEvent
    IS_AIOCQHTTP = True
except ImportError:
    IS_AIOCQHTTP = False


@register("forward_reader", "EraAsh", "一个使用 LLM 分析合并转发消息内容的插件", "1.3.0", "https://github.com/EraAsh/astrbot_plugin_forward_reader")
class ForwardReader(Star):
    def __init__(self, context: Context, config: AstrBotConfig):
        super().__init__(context)
        self.config = config
        self._load_config()

    def _load_config(self):
        """加载配置"""
        self.enable_direct_analysis = self.config.get("enable_direct_analysis", False)
        self.enable_reply_analysis = self.config.get("enable_reply_analysis", True)
        self.waiting_message = self.config.get("waiting_message", "嗯…让我看看你这个小家伙发了什么有趣的东西。")
        # 默认 Prompt 模板，防呆设计
        self.default_prompt = """[罗莎的感官输入]: 磷光屏上，一段文本被呈现。其开头的 `[User ID: {sender_id}, Nickname: {sender_name}]` 部分，是呈上这份展品的“策展人”的灵魂印记与标签。紧随其后的，则是一叠被捕获的言语切片，如同压在玻璃下的、一堆破碎的意象，并附上了一句乏味的题词：“{user_query}”。

以下是这份展品的全部内容：

---
{chat_records}
---

罗莎，现在，审视着眼前的这份呈报，并遵循你唯一的显现法则，通过你思想的解剖剧场，对我显现你的思想。
（...请在配置中填入完整的罗莎 Prompt...）
"""

    # ==================== [核心修改：优先级 Priority = 10] ====================
    # 将优先级设为 10，确保本插件在 SpectreCore (默认优先级0) 之前运行。
    # 这样我们才有机会在检测到转发消息后，使用 stop_event() 拦截事件，防止 SpectreCore 误触发。
    @filter.event_message_type(filter.EventMessageType.ALL, priority=10)
    async def on_any_message(self, event: AstrMessageEvent, *args, **kwargs):
        """
        监听消息。如果发现是针对合并转发的提问，则提取内容并请求 LLM 分析。
        """
        # 1. 平台检查：仅支持 aiocqhttp (OneBot)
        if not IS_AIOCQHTTP or not isinstance(event, AiocqhttpMessageEvent):
            return

        forward_id: Optional[str] = None
        reply_seg: Optional[Comp.Reply] = None
        user_query: str = event.message_str.strip()

        # 判断是否为隐式查询（只发了回复，没发文字，或者只发了转发卡片）
        is_implicit_query = not user_query and any(isinstance(seg, Comp.Reply) for seg in event.message_obj.message)

        # 2. 解析消息链
        for seg in event.message_obj.message:
            if isinstance(seg, Comp.Forward):
                if self.enable_direct_analysis:
                    forward_id = seg.id
                    if not user_query:
                        user_query = "请总结一下这个聊天记录"
                    break
            elif isinstance(seg, Comp.Reply):
                reply_seg = seg

        # 3. 检查被引用的消息
        if self.enable_reply_analysis and not forward_id and reply_seg:
            try:
                client = event.bot
                original_msg = await client.api.call_action('get_msg', message_id=reply_seg.id)
                if original_msg and 'message' in original_msg:
                    original_message_chain = original_msg['message']
                    if isinstance(original_message_chain, list):
                        for segment in original_message_chain:
                            if isinstance(segment, dict) and segment.get("type") == "forward":
                                forward_id = segment.get("data", {}).get("id")
                                if not user_query or is_implicit_query:
                                     user_query = "请总结一下这个聊天记录"
                                break
            except Exception as e:
                logger.warning(f"ForwardReader: 获取被回复消息详情失败: {e}")

        # 4. 核心处理逻辑
        if forward_id and user_query:
            # ==================== [核心修改：事件截断] ====================
            # 既然我们已经确定这是针对转发消息的分析请求，
            # 我们必须立即停止事件传播，防止 SpectreCore 或其他对话插件看到这个事件并响应 @Bot。
            event.stop_event()
            
            try:
                # 发送等待提示
                yield event.chain_result([Comp.Reply(id=event.message_obj.message_id), Comp.Plain(self.waiting_message)])

                # 提取转发内容
                extracted_texts, image_urls = await self._extract_forward_content(event, forward_id)
                if not extracted_texts and not image_urls:
                    yield event.plain_result("无法从合并转发消息中提取到任何有效内容。")
                    return

                chat_records = "\n".join(extracted_texts)
                
                # 获取发送者信息
                sender_name = event.get_sender_name() or "未知访客"
                sender_id = event.get_sender_id() or "unknown"

                # ==================== [核心修改：从配置加载 Prompt 并注入变量] ====================
                # 1. 读取配置中的 Prompt (conf_schema 中的 analysis_prompt)
                prompt_template = self.config.get("analysis_prompt", "")
                if not prompt_template:
                    prompt_template = self.default_prompt # 回退到硬编码的默认值
                
                # 2. 使用 replace 进行安全的变量注入
                # 相比 f-string，replace 不会因为 Prompt 中包含 JSON/CSS 的花括号 {} 而报错
                final_prompt = prompt_template.replace("{sender_name}", str(sender_name)) \
                                              .replace("{sender_id}", str(sender_id)) \
                                              .replace("{user_query}", str(user_query)) \
                                              .replace("{chat_records}", str(chat_records))

                logger.info(f"ForwardReader: 准备向 LLM 发送直接请求, Bypass Event Bus. Prompt长度: {len(final_prompt)}")

                # 3. 获取 Provider ID
                umo = event.unified_msg_origin
                provider_id = await self.context.get_current_chat_provider_id(umo=umo)
                
                # 4. 获取 Provider 实例
                provider = self.context.get_provider_by_id(provider_id)
                if not provider:
                    provider = self.context.get_using_provider() # 降级
                
                if not provider:
                    yield event.plain_result("错误：未找到可用的 LLM Provider。")
                    return

                # 5. 直接请求 (Direct Call)
                llm_response = await provider.text_chat(
                    prompt=final_prompt,
                    image_urls=image_urls,
                    contexts=[], 
                    func_tool=None,
                    system_prompt="" 
                )
                
                completion_text = llm_response.completion_text
                yield event.plain_result(completion_text)
                
            except Exception as e:
                logger.error(f"ForwardReader: 分析转发消息失败: {e}")
                yield event.plain_result(f"分析失败: {e}")

    async def _extract_forward_content(self, event: AiocqhttpMessageEvent, forward_id: str) -> tuple[list[str], list[str]]:
        """
        从合并转发消息中提取文本和图片URL。
        """
        client = event.bot
        try:
            forward_data = await client.api.call_action('get_forward_msg', id=forward_id)
        except Exception as e:
            logger.error(f"调用 get_forward_msg API 失败: {e}")
            raise ValueError("获取合并转发内容失败，可能是消息已过期或API问题。")

        if not forward_data or "messages" not in forward_data:
            raise ValueError("获取到的合并转发内容为空。")

        extracted_texts = []
        image_urls = []

        for message_node in forward_data["messages"]:
            sender_name = message_node.get("sender", {}).get("nickname", "未知用户")
            raw_content = message_node.get("message") or message_node.get("content", [])

            content_chain = []
            if isinstance(raw_content, str):
                try:
                    parsed_content = json.loads(raw_content)
                    if isinstance(parsed_content, list):
                        content_chain = parsed_content
                    else:
                        content_chain = [{"type": "text", "data": {"text": str(parsed_content)}}]
                except (json.JSONDecodeError, TypeError):
                    content_chain = [{"type": "text", "data": {"text": raw_content}}]
            elif isinstance(raw_content, list):
                content_chain = raw_content

            node_text_parts = []
            if isinstance(content_chain, list):
                for segment in content_chain:
                    if isinstance(segment, dict):
                        seg_type = segment.get("type")
                        seg_data = segment.get("data", {})
                        if seg_type == "text":
                            text = seg_data.get("text", "")
                            if text:
                                node_text_parts.append(text)
                        elif seg_type == "image":
                            url = seg_data.get("url") or seg_data.get("file") 
                            if url:
                                image_urls.append(url)
                                node_text_parts.append("[图片]")
            
            full_node_text = "".join(node_text_parts).strip()
            if full_node_text:
                extracted_texts.append(f"{sender_name}: {full_node_text}")

        return extracted_texts, image_urls

    async def terminate(self):
        pass
