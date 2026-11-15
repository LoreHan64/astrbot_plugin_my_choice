import asyncio

from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent, MessageChain, filter
from astrbot.api.message_components import Node, Nodes, Plain
from astrbot.api.star import Context, Star, register


@register("astrbot_plugin_my_choice", "AstrBot", "一个可以自动处理好友申请的插件", "1.2.0")
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

    # ------------------------------------------------------------------
    # 好友 / 群列表指令（仅管理员，默认仅私聊可用）
    # ------------------------------------------------------------------

    @filter.command("好友列表", alias={"friend_list"})
    @filter.permission_type(filter.PermissionType.ADMIN)
    async def cmd_friend_list(self, event: AstrMessageEvent):
        """以合并转发的方式展示 Bot 当前的好友列表。"""
        await self._handle_list_command(
            event,
            title="好友列表",
            unit="位好友",
            action="get_friend_list",
            formatter=self._format_friend_entry,
            empty_text="当前没有好友。",
        )

    @filter.command("群列表", alias={"group_list"})
    @filter.permission_type(filter.PermissionType.ADMIN)
    async def cmd_group_list(self, event: AstrMessageEvent):
        """以合并转发的方式展示 Bot 当前所在的群列表。"""
        await self._handle_list_command(
            event,
            title="群列表",
            unit="个群聊",
            action="get_group_list",
            formatter=self._format_group_entry,
            empty_text="当前没有加入任何群聊。",
        )

    def _list_config(self) -> dict:
        cfg = self.config.get("list_commands") or {}
        return cfg if isinstance(cfg, dict) else {}

    def _get_onebot_client(self, event: AstrMessageEvent):
        """获取 aiocqhttp 的 Bot 客户端实例，取不到则返回 None。"""
        client = getattr(event, "bot", None)
        if client is not None:
            return client
        try:
            platform = self.context.get_platform_inst(event.get_platform_id())
        except Exception as e:
            logger.error(f"[FriendRequestHandler] 获取平台实例失败: {e}")
            return None
        return getattr(platform, "bot", None)

    async def _call_onebot_api(self, event: AstrMessageEvent, action: str) -> object:
        """调用 OneBot v11 API。兼容 aiocqhttp 的两种调用方式。"""
        client = self._get_onebot_client(event)
        if client is None:
            raise RuntimeError("未找到 aiocqhttp 客户端实例")
        api = getattr(client, "api", None)
        if api is not None and hasattr(api, "call_action"):
            return await api.call_action(action)
        return await client.call_action(action)

    async def _reply(self, event: AstrMessageEvent, text: str) -> None:
        """发送一条纯文本回复，失败时只记录日志，不向用户抛出异常。"""
        try:
            await event.send(MessageChain([Plain(text)]))
        except Exception as e:
            logger.error(f"[FriendRequestHandler] 回复消息失败: {e}")

    @staticmethod
    def _to_id_text(value) -> str:
        """把 OneBot 返回的 ID 安全地转成字符串。

        这里不用 `value or ""`，否则 ID 为 0 这类假值会被误判为缺失。
        """
        if value is None:
            return ""
        return str(value).strip()

    @classmethod
    def _format_friend_entry(cls, item: dict) -> str | None:
        user_id = cls._to_id_text(item.get("user_id"))
        if not user_id:
            return None
        nickname = cls._to_id_text(item.get("nickname"))
        remark = cls._to_id_text(item.get("remark"))
        if remark and nickname and remark != nickname:
            name = f"{nickname}（备注：{remark}）"
        else:
            name = remark or nickname or "未获取到昵称"
        return f"{user_id} {name}"

    @classmethod
    def _format_group_entry(cls, item: dict) -> str | None:
        group_id = cls._to_id_text(item.get("group_id"))
        if not group_id:
            return None
        group_name = cls._to_id_text(item.get("group_name")) or "未获取到群名"
        try:
            member_count = int(item.get("member_count") or 0)
        except (TypeError, ValueError):
            member_count = 0
        return f"{group_id} {group_name}（{member_count} 人）"

    async def _handle_list_command(
        self,
        event: AstrMessageEvent,
        *,
        title: str,
        unit: str,
        action: str,
        formatter,
        empty_text: str,
    ) -> None:
        """通用的列表指令处理流程：校验 -> 拉取 -> 合并转发 -> 结束事件。"""
        try:
            cfg = self._list_config()

            if not cfg.get("enable_list_commands", True):
                await self._reply(event, "管理员已关闭列表查询功能。")
                return

            if event.get_platform_name() != "aiocqhttp":
                await self._reply(event, "该指令仅支持 aiocqhttp (OneBot v11) 平台。")
                return

            if cfg.get("list_private_only", True) and not event.is_private_chat():
                await self._reply(event, f"为避免泄露信息，{title}仅支持在私聊中使用，请私聊我后再发送指令。")
                return

            try:
                raw = await asyncio.wait_for(self._call_onebot_api(event, action), timeout=60)
            except Exception as e:
                logger.error(f"[FriendRequestHandler] 调用 {action} 失败: {e}")
                await self._reply(event, f"获取{title}失败：{e}")
                return

            if isinstance(raw, dict):
                # 部分实现会把列表包在 data 字段里。
                raw = raw.get("data") if isinstance(raw.get("data"), list) else None

            if not isinstance(raw, list):
                logger.error(f"[FriendRequestHandler] {action} 返回了非列表结果: {type(raw)}")
                await self._reply(event, f"获取{title}失败：接口返回数据格式异常。")
                return

            lines: list[str] = []
            for item in raw:
                if not isinstance(item, dict):
                    continue
                line = formatter(item)
                if line:
                    lines.append(line)

            if not lines:
                await self._reply(event, empty_text)
                return

            total = len(lines)
            max_entries = cfg.get("list_max_entries", 500)
            truncated = False
            try:
                max_entries = int(max_entries)
            except (TypeError, ValueError):
                max_entries = 500
            if max_entries > 0 and total > max_entries:
                lines = lines[:max_entries]
                truncated = True

            try:
                per_node = int(cfg.get("list_entries_per_node", 20))
            except (TypeError, ValueError):
                per_node = 20
            per_node = max(1, min(per_node, 200))

            uin, nickname = await self._get_bot_identity(event)
            suffix = f"，仅显示前 {len(lines)} 条）" if truncated else "）"
            header = f"{title}（共 {total} {unit}{suffix}"

            nodes = [Node(content=[Plain(header)], name=nickname, uin=uin)]
            for i in range(0, len(lines), per_node):
                chunk = lines[i : i + per_node]
                nodes.append(
                    Node(content=[Plain("\n".join(chunk))], name=nickname, uin=uin)
                )

            try:
                await event.send(MessageChain([Nodes(nodes)]))
            except Exception as e:
                logger.error(f"[FriendRequestHandler] 发送{title}合并转发失败: {e}")
                fallback = "\n".join(lines[:50])
                await self._reply(event, f"{header}\n{fallback}\n（合并转发发送失败，已降级为文本：{e}）")
        except Exception as e:
            logger.error(f"[FriendRequestHandler] 处理{title}指令异常: {e}")
        finally:
            # 指令类事件不应继续交给 LLM 处理。
            event.stop_event()

    async def _get_bot_identity(self, event: AstrMessageEvent) -> tuple[str, str]:
        """获取合并转发节点使用的 QQ 号与昵称。"""
        uin = str(event.get_self_id() or "0")
        nickname = "Bot"
        try:
            info = await asyncio.wait_for(
                self._call_onebot_api(event, "get_login_info"), timeout=15
            )
            if isinstance(info, dict):
                uin = str(info.get("user_id") or uin)
                nickname = str(info.get("nickname") or nickname)
        except Exception as e:
            logger.debug(f"[FriendRequestHandler] 获取 Bot 登录信息失败，使用默认值: {e}")
        return uin, nickname

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
