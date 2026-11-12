from astrbot.api.event import filter, AstrMessageEvent
from astrbot.api.star import Context, Star, register
from astrbot.api import logger
from astrbot.api.message_components import Plain
from astrbot.api.event import MessageChain

@register("astrbot_plugin_my_choice", "AstrBot", "一个可以自动处理好友申请的插件", "1.1.3")
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
        # OneBot 允许 comment / user_id 为 null，直接拿去做 str.replace / replace 会抛 TypeError。
        comment = str(raw_message.get("comment") or "")
        user_id = str(raw_message.get("user_id") or "")

        mode = self.config.get("handling_mode", "llm_chat")

        logger.info(f"[FriendRequestHandler] 收到来自 {user_id} 的好友申请，验证消息：{comment}，处理模式：{mode}")

        approve = False
        result_text = "拒绝"
        reason = "不适用"

        if mode == "auto_accept":
            approve = True
            result_text = "自动同意"
        elif mode == "auto_reject":
            approve = False
            result_text = "自动拒绝"
        elif mode == "llm_chat":
            approve, reason = await self._decide_by_llm_chat(user_id, comment, event)
            result_text = "聊天模型判定为同意" if approve else "聊天模型判定为拒绝"

        # 执行操作
        try:
            if not flag:
                raise ValueError("好友申请事件缺少 flag 字段，无法下发处理结果")
            client = event.bot  # 因为 get_platform_name() == 'aiocqhttp'，直接获取 client 并下发。
            await client.api.call_action("set_friend_add_request", flag=str(flag), approve=approve)
            logger.info(f"[FriendRequestHandler] 已处理好友申请 (user_id={user_id})，结果：{approve}")
        except Exception as e:
            logger.error(f"[FriendRequestHandler] 处理好友申请失败: {e}")
            result_text = f"处理失败 ({e})"

        # 转发通知
        fwd_settings = self.config.get("forward_settings", {}) or {}
        if fwd_settings.get("enable_forward", False):
            target_umo = fwd_settings.get("forward_target_umo", "")
            if target_umo:
                template = fwd_settings.get("forward_template") or "收到来自 {user_id} 的好友申请，验证消息为：{comment}。\n处理结果：{result}\n判定理由：{reason}"
                msg_text = template.replace("{user_id}", user_id).replace("{comment}", comment).replace("{result}", result_text).replace("{reason}", str(reason))
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
                # get_persona_v3_by_id 是同步方法，不能 await，否则这里会抛 TypeError
                # 并被下面的 except 吞掉，导致「处理好友申请的人设」配置永远不生效。
                persona = self.context.persona_manager.get_persona_v3_by_id(persona_id)
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

    async def _decide_by_llm_chat(self, user_id: str, comment: str, event: AstrMessageEvent) -> tuple[bool, str]:
        import json
        import re

        provider_id = self.config.get("chat_provider", "")

        prompt_template = self.config.get("llm_prompt", "用户 {user_id} 申请添加你为好友，验证留言为：「{comment}」。请根据你的人设判断是否同意该好友申请。\n请务必以 JSON 格式回复，不要包含其他无关内容。JSON 的格式如下：\n{\n  \"decision\": \"APPROVE\" 或 \"REJECT\",\n  \"reason\": \"你决定同意或拒绝的具体理由\"\n}")
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
                return False, "获取模型失败"

            llm_resp = await self.context.llm_generate(
                chat_provider_id=chat_provider_id,
                prompt=prompt,
                system_prompt=system_prompt,
                session_id=f"friend_req_{user_id}"
            )
            resp_text = llm_resp.completion_text or ""
            logger.info(f"[FriendRequestHandler] 聊天模型返回：{resp_text}")

            # 解析返回的JSON
            decision = False
            reason = "未知理由"
            try:
                # 尝试通过正则提取大括号中的内容以避免markdown backticks等影响
                match = re.search(r'\{.*\}', resp_text, re.DOTALL)
                if match:
                    json_str = match.group(0)
                else:
                    json_str = resp_text

                parsed_json = json.loads(json_str)
                if "decision" in parsed_json:
                    decision = "APPROVE" in str(parsed_json.get("decision", "")).upper()
                else:
                    # 容错：如果JSON没有 decision 字段，退化到文本匹配
                    decision = "APPROVE" in resp_text.upper()

                reason = parsed_json.get("reason", "模型未给出具体理由")
            except Exception as json_e:
                logger.error(f"[FriendRequestHandler] 解析聊天模型JSON失败: {json_e}")
                decision = "APPROVE" in resp_text.upper()
                reason = "无法解析模型返回的理由"

            return decision, reason
        except Exception as e:
            logger.error(f"[FriendRequestHandler] 请求聊天模型失败: {e}")
            return False, f"请求模型失败: {e}"
