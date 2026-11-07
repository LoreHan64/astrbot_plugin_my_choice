import asyncio
from astrbot.api.event import filter, AstrMessageEvent, MessageEventResult
from astrbot.api.star import Context, Star, register
from astrbot.api import logger
from astrbot.api.message_components import Plain
from astrbot.api.event import MessageChain
from astrbot.core.platform.sources.aiocqhttp.aiocqhttp_message_event import AiocqhttpMessageEvent
from astrbot.core.provider.entities import ProviderType
from astrbot.core.provider import Provider

@register("astrbot_plugin_my_choice", "AstrBot", "一个可以自动处理好友申请的插件", "1.0.2")
class FriendRequestHandlerPlugin(Star):
    def __init__(self, context: Context, config: dict):
        super().__init__(context)
        self.config = config

    # Friend request events are wrapped as OTHER_MESSAGE / FRIEND_MESSAGE depending on internal router, so ALL covers it.
    @filter.event_message_type(filter.EventMessageType.ALL)
    async def on_other_message(self, event: AstrMessageEvent):
        if event.get_platform_name() != "aiocqhttp":
            return

        raw_message = event.message_obj.raw_message
        if not isinstance(raw_message, dict):
            return

        # 判断是否为好友申请事件
        if raw_message.get("post_type") == "request" and raw_message.get("request_type") == "friend":
            await self._handle_friend_request(event, raw_message)
            event.stop_event()

    async def _handle_friend_request(self, event: AstrMessageEvent, raw_message: dict):
        flag = raw_message.get("flag")
        comment = raw_message.get("comment", "")
        user_id = str(raw_message.get("user_id", ""))

        mode = self.config.get("handling_mode", "llm_chat")

        logger.info(f"[FriendRequestHandler] 收到来自 {user_id} 的好友申请，验证消息：{comment}，处理模式：{mode}")

        approve = False
        result_text = "拒绝"

        if mode == "auto_accept":
            approve = True
            result_text = "自动同意"
        elif mode == "auto_reject":
            approve = False
            result_text = "自动拒绝"
        elif mode == "llm_chat":
            approve = await self._decide_by_llm_chat(user_id, comment, event)
            result_text = "聊天模型判定为同意" if approve else "聊天模型判定为拒绝"
        elif mode == "llm_decision":
            approve = await self._decide_by_llm_decision(user_id, comment, event)
            result_text = "判断模型判定为同意" if approve else "判断模型判定为拒绝"

        # 执行操作
        try:
            client = event.bot  # 因为 get_platform_name() == 'aiocqhttp'，直接获取 client 并下发。
            await client.api.call_action("set_friend_add_request", flag=flag, approve=approve)
            logger.info(f"[FriendRequestHandler] 已处理好友申请 (user_id={user_id})，结果：{approve}")
        except Exception as e:
            logger.error(f"[FriendRequestHandler] 处理好友申请失败: {e}")
            result_text = f"处理失败 ({e})"

        # 转发通知
        fwd_settings = self.config.get("forward_settings", {})
        if fwd_settings.get("enable_forward", False):
            target_umo = fwd_settings.get("forward_target_umo", "")
            if target_umo:
                template = fwd_settings.get("forward_template", "收到来自 {user_id} 的好友申请，验证消息为：{comment}。处理结果：{result}")
                msg_text = template.replace("{user_id}", user_id).replace("{comment}", comment).replace("{result}", result_text)
                try:
                    chain = MessageChain([Plain(msg_text)])
                    await self.context.send_message(target_umo, chain)
                    logger.info(f"[FriendRequestHandler] 已发送转发通知给 {target_umo}")
                except Exception as e:
                    logger.error(f"[FriendRequestHandler] 转发通知失败: {e}")

    async def _get_system_prompt(self, event: AstrMessageEvent) -> str:
        persona_id = self.config.get("persona_id", "")
        persona = None
        try:
            if persona_id:
                persona = await self.context.persona_manager.get_persona_v3_by_id(persona_id)
            else:
                persona = await self.context.persona_manager.get_default_persona_v3(umo=event.unified_msg_origin)
                if not persona:
                    persona = await self.context.persona_manager.get_default_persona_v3(umo=None)
        except Exception as e:
            logger.error(f"[FriendRequestHandler] 获取人设失败: {e}")
            try:
                persona = await self.context.persona_manager.get_default_persona_v3(umo=None)
            except Exception:
                pass

        return persona.get("prompt", "") if isinstance(persona, dict) else ""

    async def _decide_by_llm_chat(self, user_id: str, comment: str, event: AstrMessageEvent) -> bool:
        provider_id = self.config.get("chat_provider", "")

        prompt_template = self.config.get("llm_prompt", "用户 {user_id} 申请添加你为好友，验证留言为：「{comment}」。请根据你的人设判断是否同意该好友申请。如果同意请回复 APPROVE，如果拒绝请回复 REJECT。仅回复这两个单词之一即可。")
        prompt = prompt_template.replace("{user_id}", user_id).replace("{comment}", comment)

        try:
            system_prompt = await self._get_system_prompt(event)

            if provider_id:
                provider = await self.context.provider_manager.get_provider_by_id(provider_id)
                chat_provider_id = provider_id if provider else None
            else:
                chat_provider_id = await self.context.get_current_chat_provider_id(umo=event.unified_msg_origin)

            if not chat_provider_id:
                 logger.error("[FriendRequestHandler] 找不到可用的聊天模型")
                 return False

            llm_resp = await self.context.llm_generate(
                chat_provider_id=chat_provider_id,
                prompt=prompt,
                system_prompt=system_prompt,
                session_id=f"friend_req_{user_id}"
            )
            resp_text = llm_resp.completion_text.upper()
            logger.info(f"[FriendRequestHandler] 聊天模型返回：{resp_text}")
            if "APPROVE" in resp_text:
                return True
            return False
        except Exception as e:
            logger.error(f"[FriendRequestHandler] 请求聊天模型失败: {e}")
            return False

    async def _decide_by_llm_decision(self, user_id: str, comment: str, event: AstrMessageEvent) -> bool:
        provider_id = self.config.get("decision_provider", "")

        prompt_template = self.config.get("llm_prompt", "用户 {user_id} 申请添加你为好友，验证留言为：「{comment}」。请根据你的人设判断是否同意该好友申请。")
        question = prompt_template.replace("{user_id}", user_id).replace("{comment}", comment)

        try:
            system_prompt = await self._get_system_prompt(event)

            # 兼容：如果 state 尚未有效覆盖人设，也可以拼接到问题前：
            if system_prompt:
                question = f"[系统人设/前提]: {system_prompt}\n\n[判断问题]: {question}"

            # 使用内置的 decision 判定
            approve = await self.context.decision.yes_no(
                question=question,
                provider_id=provider_id if provider_id else None,
                threshold=0.5
            )
            logger.info(f"[FriendRequestHandler] 判断模型返回：{approve}")
            return approve
        except Exception as e:
            logger.error(f"[FriendRequestHandler] 请求判断模型失败: {e}")
            return False
