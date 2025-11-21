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


@register("forward_reader", "EraAsh", "一个使用 LLM 分析合并转发消息内容的插件", "1.4.1", "https://github.com/EraAsh/astrbot_plugin_forward_reader")
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
        # 仅保留文本长度软限制，防止 Context Window 溢出导致 API 报错
        self.max_text_length = 15000 

    @filter.event_message_type(filter.EventMessageType.ALL, priority=10)
    async def on_any_message(self, event: AstrMessageEvent, *args, **kwargs):
        if not IS_AIOCQHTTP or not isinstance(event, AiocqhttpMessageEvent):
            return

        forward_id: Optional[str] = None
        reply_seg: Optional[Comp.Reply] = None
        user_query: str = event.message_str.strip()
        is_implicit_query = not user_query and any(isinstance(seg, Comp.Reply) for seg in event.message_obj.message)

        for seg in event.message_obj.message:
            if isinstance(seg, Comp.Forward):
                if self.enable_direct_analysis:
                    forward_id = seg.id
                    if not user_query: user_query = "请总结一下这个聊天记录"
                    break
            elif isinstance(seg, Comp.Reply):
                reply_seg = seg

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
                                if not user_query or is_implicit_query: user_query = "请总结一下这个聊天记录"
                                break
            except Exception as e:
                logger.warning(f"ForwardReader: 获取被回复消息详情失败: {e}")

        if forward_id and user_query:
            event.stop_event() # 截断事件
            
            try:
                yield event.chain_result([Comp.Reply(id=event.message_obj.message_id), Comp.Plain(self.waiting_message)])

                # 提取内容 (不再限制图片数量)
                extracted_texts, image_urls = await self._extract_forward_content(event, forward_id)
                
                if not extracted_texts and not image_urls:
                    yield event.plain_result("无法从合并转发消息中提取到任何有效内容。")
                    return

                # 注入数据构建
                chat_records_str = "\n".join(extracted_texts)
                
                # 文本过长截断 (这个建议保留，防止几万字的小说直接把 LLM 撑爆)
                if len(chat_records_str) > self.max_text_length:
                    chat_records_str = chat_records_str[:self.max_text_length] + "\n\n[...系统提示：由于篇幅过长，后续内容已被截断...]"

                chat_records_injection = f"<chat_log>\n{chat_records_str}\n</chat_log>"

                sender_name = event.get_sender_name() or "未知访客"
                sender_id = event.get_sender_id() or "unknown"

                # 加载 Prompt
                prompt_template = self.config.get("analysis_prompt", "")
                if not prompt_template:
                    prompt_template = """[罗莎的感官输入]: 磷光屏上，一段文本被呈现。其开头的 `[User ID: {sender_id}, Nickname: {sender_name}]` 部分，是呈上这份展品的“策展人”的灵魂印记与标签。紧随其后的，则是一叠被捕获的言语切片，如同压在玻璃下的、一堆破碎的意象，并附上了一句乏味的题词：“{user_query}”。

以下是这份展品的全部内容，已被封装在 <chat_log> 标签中，请注意其中的 [图片x] 标签对应上传的第 x 张图片：

{chat_records}

罗莎，现在，审视着眼前的这份呈报...
"""
                
                final_prompt = prompt_template.replace("{sender_name}", str(sender_name)) \
                                              .replace("{sender_id}", str(sender_id)) \
                                              .replace("{user_query}", str(user_query)) \
                                              .replace("{chat_records}", chat_records_injection)

                logger.info(f"ForwardReader: 请求分析, 文本长度: {len(chat_records_str)}, 图片数: {len(image_urls)}")

                umo = event.unified_msg_origin
                provider_id = await self.context.get_current_chat_provider_id(umo=umo)
                provider = self.context.get_provider_by_id(provider_id) or self.context.get_using_provider()
                
                if not provider:
                    yield event.plain_result("错误：未找到可用的 LLM Provider。")
                    return

                llm_response = await provider.text_chat(
                    prompt=final_prompt,
                    image_urls=image_urls, 
                    contexts=[],
                    func_tool=None
                )
                
                yield event.plain_result(llm_response.completion_text)
                
            except Exception as e:
                logger.error(f"ForwardReader: 分析失败: {e}")
                yield event.plain_result(f"分析失败: {e}")

    async def _extract_forward_content(self, event: AiocqhttpMessageEvent, forward_id: str) -> tuple[list[str], list[str]]:
        """
        提取逻辑：保留图片索引编号，移除数量上限
        """
        client = event.bot
        try:
            forward_data = await client.api.call_action('get_forward_msg', id=forward_id)
        except Exception as e:
            raise ValueError(f"获取合并转发内容失败: {e}")

        if not forward_data or "messages" not in forward_data:
            raise ValueError("获取到的合并转发内容为空。")

        extracted_texts = []
        image_urls = []
        
        img_count = 0

        for message_node in forward_data["messages"]:
            sender_name = message_node.get("sender", {}).get("nickname", "未知用户")
            raw_content = message_node.get("message") or message_node.get("content", [])

            content_chain = []
            if isinstance(raw_content, str):
                try:
                    parsed_content = json.loads(raw_content)
                    if isinstance(parsed_content, list): content_chain = parsed_content
                    else: content_chain = [{"type": "text", "data": {"text": str(parsed_content)}}]
                except: content_chain = [{"type": "text", "data": {"text": raw_content}}]
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
                            if text: node_text_parts.append(text)
                        elif seg_type == "image":
                            url = seg_data.get("url") or seg_data.get("file")
                            if url:
                                img_count += 1
                                image_urls.append(url)
                                # 核心保留：索引编号，帮助 LLM 定位图片位置
                                node_text_parts.append(f"[图片{img_count}]")
            
            full_node_text = "".join(node_text_parts).strip()
            if full_node_text:
                extracted_texts.append(f"{sender_name}: {full_node_text}")

        return extracted_texts, image_urls

    async def terminate(self):
        pass
