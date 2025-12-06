import asyncio
import json
import os
import re
import time
import traceback
from asyncio import Queue
from datetime import datetime
from typing import List
from xml.etree.ElementTree import Element

import aiohttp
import websockets
from lxml import etree

from astrbot.core.message.components import At, Plain
from astrbot.core.message.message_event_result import MessageChain
from astrbot.core.platform import PlatformMetadata, AstrBotMessage, MessageType, MessageMember, AstrMessageEvent, \
    Platform
from astrbot.core.platform.message_session import MessageSesion
from astrbot.core.platform.register import register_platform_adapter
from astrbot.core.star.context import logger

from .wechat_income_msg import WechatWebsocketSysMsgType, WechatIncomeMsgType, WechatChaosMsgBaseType  # noqa
from .wechat_websocket_message_event import WeChatWebsocketMessageEvent # noqa


# TODO: 完善代码
@register_platform_adapter(
    adapter_name="wechat-websocket",
    adapter_display_name="微信个人hook",
    desc="微信个人hook",
    default_config_tmpl={
        "host": "目标IP",
        "port": "5555",
    },
    logo_path="assets/wechat-6a207b66.png",
    support_streaming_message=False,
)
class WeChatWebsocketAdapter(Platform):

    def __init__(
        self,
        platform_config: dict,
        platform_settings: dict,
        event_queue: Queue
    ) -> None:
        super().__init__(event_queue)
        self._shutdown_event = None
        self.config = platform_config
        self.settings = platform_settings

        self.unique_session = platform_settings["unique_session"]

        self.metadata = PlatformMetadata(
            name="wechat-websocket",
            description="微信机器人个人适配",
            id=self.config.get("id", "wechat-websocket"),
            support_streaming_message=False,
        )

        self.host = self.config.get("host")
        self.port = self.config.get("port")
        self.active_message_poll: bool = self.config.get(
            "ww_active_message_poll",
            False,
        )
        self.active_message_poll_interval: int = self.config.get(
            "ww_active_message_poll_interval",
            5,
        )
        self.base_url = f"http://{self.host}:{self.port}"
        self.wxid = None
        self.nickname = None
        self.head_pic = None
        self.ws_handle_task = None

        # 添加文本消息缓存，用于引用消息处理
        """缓存文本消息。key是NewMsgId (对应引用消息的svrid)，value是消息文本内容"""
        self.cached_texts = {}
        # 设置文本缓存大小限制
        self.max_text_cache = 100

    async def run(self) -> None:
        """启动平台适配器的运行实例。"""
        logger.info(f"{self.metadata.name} 适配器正在启动...")

        isLoginIn = await self.check_online_status()
        if (isLoginIn):
            self.ws_handle_task = asyncio.create_task(self.connect_websocket())
        self._shutdown_event = asyncio.Event()
        await self._shutdown_event.wait()
        logger.info(f"{self.metadata.name} 适配器已停止。")

    def meta(self) -> PlatformMetadata:
        return PlatformMetadata(
            name="wechat_websocket",
            description="微信机器人个人适配",
            id=self.config.get("id")
        )

    async def send_by_session(self,
                              session: MessageSesion,
                              message_chain: MessageChain):
        dummy_message_obj = AstrBotMessage()
        dummy_message_obj.session_id = session.session_id
        # 根据 session_id 判断消息类型
        if "@chatroom" in session.session_id:
            dummy_message_obj.type = MessageType.GROUP_MESSAGE
            if "#" in session.session_id:
                dummy_message_obj.group_id = session.session_id.split("#")[0]
            else:
                dummy_message_obj.group_id = session.session_id
            dummy_message_obj.sender = MessageMember(user_id="", nickname="")
        else:
            dummy_message_obj.type = MessageType.FRIEND_MESSAGE
            dummy_message_obj.group_id = ""
            dummy_message_obj.sender = MessageMember(user_id="", nickname="")
        sending_event = WeChatWebsocketMessageEvent(
            message_str="",
            message_obj=dummy_message_obj,
            platform_meta=self.meta(),
            session_id=session.session_id,
            adapter=self,
        )
        await sending_event.send(message_chain)

    def commit_event(self, event: AstrMessageEvent):
        super().commit_event(event)

    def get_client(self):
        super().get_client()

    async def check_online_status(self):
        url = f"{self.base_url}/api/get_personal_info"
        data = {
            "para": {
                "id": f"{int(datetime.now().timestamp())}",
                "type": 6500,
                "roomid": "",
                "wxid": "",
                "content": "",
                "nickname": "",
                "ext": ""
            }
        }
        async with aiohttp.ClientSession() as session:
            try:
                async with session.get(url, json=data) as response:
                    response_data = await response.text()
                    response_data = json.loads(response_data)
                    if response.status == 200 and response_data.get("status") == "SUCCSESSED":
                        person_info = json.loads(response_data.get("content"))
                        self.wxid = person_info.get("wx_id")
                        self.nickname = person_info.get("wx_name")
                        self.head_pic = person_info.get("wx_head_image")
                        return True
            except aiohttp.ClientConnectorError as e:
                logger.error(f"连接到 wechat_websocket 服务失败: {e}")
                return False
            except Exception as e:
                logger.error(f"检查 wechat_websocket 在线状态时发生错误: {e}")
                logger.error(traceback.format_exc())
                return False

    async def connect_websocket(self):
        os.environ["no_proxy"] = f"localhost,127.0.0.1,{self.host}"
        ws_url = f"ws://{self.host}:{self.port}"
        logger.info(f"{self.metadata.name} 正在连接 WebSocket: {ws_url}")
        while True:
            try:
                async with websockets.connect(ws_url) as ws:
                    logger.debug(f"{self.metadata.name} WebSocket 连接成功。")
                    wait_time = (
                        self.active_message_poll_interval
                        if self.active_message_poll
                        else 120
                    )
                    while True:
                        try:
                            message = await asyncio.wait_for(ws.recv(), wait_time)
                            asyncio.create_task(self.handle_websocket_message(message))
                        except asyncio.TimeoutError:
                            logger.debug(f"WebSocket 连接空闲超过 {wait_time} s")
                            break
                        except websockets.exceptions.ConnectionClosedOK:
                            logger.info("WebSocket 连接正常关闭。")
                            break
                        except Exception as e:
                            logger.error(f"处理 WebSocket 消息时发生错误: {e}")
                            break
            except Exception as e:
                logger.error(
                    f"WebSocket 连接失败: {e}, 请检查{self.metadata.name}服务状态，或尝试重启{self.metadata.name}适配器。",
                )
                await asyncio.sleep(5)

    async def handle_websocket_message(self, message: str):
        """处理从 WebSocket 接收到的消息。"""
        logger.debug(f"收到 WebSocket 消息: {message}")
        try:
            message_data = json.loads(message)
            msg_type = message_data.get("type")
            if msg_type == WechatIncomeMsgType.HEART_BEAT.value:
                logger.debug("收到心跳消息")
                return
            if msg_type == WechatIncomeMsgType.ON_CONNECT.value:
                logger.info("websocket 成功连接")
                return
            if msg_type == WechatIncomeMsgType.PIC.value:
                logger.error(f"收到图片消息，暂未实现。{message}")
                return
            if (msg_type != WechatIncomeMsgType.TXT.value and
                msg_type != WechatIncomeMsgType.CHAOS.value):
                logger.error(f"收到非文字消息：{message}")
                return
            if message_data.get("id") is not None:
                abm = await self.convert_message(message_data)
                if abm:
                    message_event = WeChatWebsocketMessageEvent(
                        message_str=abm.message_str,
                        message_obj=abm,
                        platform_meta=self.meta(),
                        session_id=abm.session_id,
                        # 传递适配器实例，以便在事件中调用 send 方法
                        adapter=self,
                    )
                    # 提交事件到事件队列
                    self.commit_event(message_event)
            else:
                logger.warning(f"收到未知结构的 WebSocket 消息: {message_data}")
        except json.JSONDecodeError:
            logger.error(f"无法解析 WebSocket 消息为 JSON: {message}")
        except Exception as e:
            logger.error(f"处理 WebSocket 消息时发生错误: {e}")

    async def convert_message(self, raw_message: dict) -> AstrBotMessage | None:
        """将 WeChat-Websocket 原始消息转换为 AstrBotMessage。"""
        abm = AstrBotMessage()
        abm.raw_message = raw_message
        abm.message_id = str(raw_message.get("id"))
        abm.timestamp = int(time.mktime(time.strptime(raw_message.get("time"), "%Y-%m-%d %H:%M:%S")))
        abm.self_id = self.wxid

        if int(time.time()) - abm.timestamp > 180:
            logger.warning(
                f"忽略 3 分钟前的旧消息：消息时间戳 {abm.timestamp} 超过当前时间 {int(time.time())}。",
            )
            return None

        content = raw_message.get("content", "")
        from_group_id = raw_message.get("wxid", "" if isinstance(content, str) else content.get("id1", ""))
        from_user_id = raw_message.get("wxid", "" if isinstance(content, str) else content.get("id2", ""))
        if from_group_id != "" and "@chatroom" not in from_group_id:
            from_user_id = from_group_id
            from_group_id = ""
        msg_type = raw_message.get("type")

        abm.message_str = ""
        abm.message = []

        if from_user_id == self.wxid:
            logger.info("忽略来自自己的消息。")
            return None

        if from_user_id in ["weixin", "newsapp", "newsapp_wechat"]:
            logger.info("忽略来自微信团队的消息。")
            return None
        if await self._process_chat_type(
            abm,
            raw_message,
            from_user_id,
            from_group_id,
        ):
            await self._process_message_content(abm, raw_message, msg_type, content)
            return abm
        return None

    async def _process_chat_type(self, abm, raw_message: dict, from_user_id, from_group_id):
        """判断消息是群聊还是私聊，并设置 AstrBotMessage 的基本属性。"""
        if from_user_id == "weixin":
            return False
        # at_me = False
        if "@chatroom" in from_group_id:
            abm.type = MessageType.GROUP_MESSAGE
            abm.group_id = from_group_id

            sender_wxid = from_user_id
            abm.sender = MessageMember(user_id=sender_wxid, nickname="")

            # 获取群聊发送者的nickname
            if sender_wxid:
                accurate_nickname = await self._get_group_member_nickname(
                    abm.group_id,
                    sender_wxid,
                )
                if accurate_nickname:
                    abm.sender.nickname = accurate_nickname

            # 对于群聊，session_id 可以是群聊 ID 或发送者 ID + 群聊 ID (如果 unique_session 为 True)
            if self.unique_session:
                abm.session_id = f"{from_group_id}#{abm.sender.user_id}"
            else:
                abm.session_id = from_group_id

            # todo 处理 xml 内容
            # msg_source = raw_message.get("other", "")
            # if self.wxid in msg_source:
            #     at_me = True
            # if "在群聊中@了你" in raw_message.get("push_content", ""):
            #     at_me = True
            # if at_me:
            #     abm.message.insert(0, At(qq=abm.self_id, name=""))
        else:
            abm.type = MessageType.FRIEND_MESSAGE
            abm.group_id = ""
            nick_name = ""
            # if push_content and " : " in push_content:
            #     nick_name = push_content.split(" : ")[0]
            abm.sender = MessageMember(user_id=from_user_id, nickname=nick_name)
            abm.session_id = from_user_id
        return True

    async def _get_group_member_nickname(self, group_id, sender_wxid) -> str | None:
        """通过接口获取群成员的昵称。"""
        url = f"{self.base_url}/api/getmembernick"
        payload = {
            "para": {
                "id": f"{int(datetime.now().timestamp())}",
                "type": 5020,
                "roomid": f"{group_id}",
                "wxid": f"{sender_wxid}",
                "content": "null",
                "nickname": "null",
                "ext": "null"
            }
        }
        async with aiohttp.ClientSession() as session:
            try:
                async with session.post(url, json=payload) as response:
                    response_text = await response.text()
                    response_data = json.loads(response_text)
                    if response.status == 200 and response_data.get("status") == "SUCCSESSED":
                        content_json = response_data.get("content")
                        content = json.loads(content_json)
                        return content.get("nick")
                    else:
                        logger.error(
                            f"获取群成员详情失败: {response.status}, {response_data}",
                        )
                        return None
            except aiohttp.ClientConnectorError as e:
                logger.error(f"连接到 {self.metadata.name} 服务失败: {e}")
                return None
            except Exception as e:
                logger.error(f"获取群成员详情时发生错误: {e}")
                return None

    async def _process_message_content(self, abm: AstrBotMessage, raw_message: dict, msg_type: int, content: str):
        """根据消息类型处理消息内容，填充 AstrBotMessage 的 message 列表。"""
        if WechatIncomeMsgType.CHAOS.value == msg_type:
            await self._process_chaos(abm, raw_message)
        elif WechatIncomeMsgType.TXT.value == msg_type:
            abm.message_str = content
            if abm.type == MessageType.GROUP_MESSAGE:
                # 检查是否@了机器人，参考 gewechat 的实现方式
                # 微信大部分客户端在@用户昵称后面，紧接着是一个\u2005字符（四分之一空格）
                at_me = False

                # 检查 other 中是否包含机器人的 wxid
                other_xml = raw_message.get("other")
                at_user_list = None
                if other_xml:
                    xml_data = etree.fromstring(other_xml)
                    at_user_list: List[Element] | None = xml_data.xpath("/msgsource/atuserlist")
                    # 用户列表通过 , 分割
                    at_user_list: str | None = at_user_list[0].text if at_user_list else None

                if at_user_list and self.wxid in at_user_list:
                    at_me = True
                if at_me:
                    # 被@了，在消息开头插入At组件
                    bot_nickname = await self._get_group_member_nickname(
                        abm.group_id,
                        self.wxid
                    )
                    abm.message.insert(
                        0,
                        At(qq=abm.self_id, name=bot_nickname or abm.self_id)
                    )
                    # 只有当消息内容不仅仅是@时才添加Plain组件
                    other_worlds = re.sub(r" ?@[^ ]{0,50} ?", "", content)
                    if len(other_worlds.strip()) > 1:
                        abm.message.append(Plain(content))
                    else:
                        # 检查是否只包含@机器人
                        is_pure_at = False
                        if (
                                bot_nickname
                                and content.strip() == f"@{bot_nickname}"
                        ):
                            is_pure_at = True
                        if not is_pure_at:
                            abm.message.append(Plain(content))
                else:
                    # 没有@机器人，作为普通文本处理
                    abm.message.append(Plain(content))
            else:
                # 私聊消息
                abm.message.append(Plain(abm.message_str))
        # todo 处理其他类型消息
        else:
            logger.warning(f"收到未处理的消息类型: {msg_type}。")
            return
        try:
            # 获取msg_id作为缓存的key
            new_msg_id = raw_message.get("id")
            if new_msg_id:
                # 限制缓存大小
                if (
                        len(self.cached_texts) >= self.max_text_cache
                        and self.cached_texts
                ):
                    # 删除最早的一条缓存
                    oldest_key = next(iter(self.cached_texts))
                    self.cached_texts.pop(oldest_key)
            logger.debug(f"缓存文本消息，new_msg_id={new_msg_id}")
            self.cached_texts[str(new_msg_id)] = content
        except Exception as e:
            logger.error(f"缓存文本消息失败: {e}")

    async def _process_chaos(self, abm: AstrBotMessage, raw_message: dict):
        # todo 处理被引用的消息
        chaos_xml = raw_message.get("content").get("content")
        xml_data = etree.fromstring(chaos_xml)
        xml_type_list = xml_data.xpath("/msg/appmsg/type")
        xml_type: int | None = int(xml_type_list[0].text) if xml_type_list else None
        if xml_type is not None and xml_type != WechatChaosMsgBaseType.REF.value:
            logger.warning(f"不是引用消息，不予处理，{raw_message}")
            return
        ref_type_list = xml_data.xpath("/msg/appmsg/refermsg/type")
        ref_type: int | None = int(ref_type_list[0].text) if ref_type_list else None
        if ref_type not in [WechatIncomeMsgType.TXT.value, WechatIncomeMsgType.PIC.value]:
            logger.warning(f"引用除了文字和图片的消息，不予处理，{raw_message}")
            return
        title_list = xml_data.xpath("/msg/appmsg/title")
        title: str | None = title_list[0].text if title_list else None
        content = title
        abm.message_str = content
        at_list = re.findall(rf"( ?@[^ ]{0,50} ?)", content)
        at_me = False
        bot_nickname = None
        if len(at_list) > 0:
            bot_nickname = await self._get_group_member_nickname(
                abm.group_id,
                self.wxid
            )
            for at in at_list:
                if bot_nickname in at and" " in at:
                    at_me = True
        if at_me:
            abm.message.insert(
                0,
                At(qq=abm.self_id, name=bot_nickname or abm.self_id)
            )
        abm.message.append(Plain(content))



    async def terminate(self):
        """终止一个平台的运行实例。"""
        logger.info(f"终止 {self.metadata.name} 适配器。")
        try:
            if self.ws_handle_task:
                self.ws_handle_task.cancel()
            self._shutdown_event.set()
        except Exception:
            pass

    async def get_contact_list(self):
        """获取联系人列表。"""
        # TODO
        logger.error("未实现 get_contact_list")
        return None

    async def get_contact_details_list(
            self,
            room_wx_id_list: list[str] = None,
            user_names: list[str] = None,
    ) -> dict | None:
        """获取联系人详情列表。"""
        # TODO
        logger.error("未实现 get_contact_details_list")
        return None