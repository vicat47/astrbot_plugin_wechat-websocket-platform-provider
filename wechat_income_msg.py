import json
from dataclasses import dataclass, field
from enum import Enum
from typing import Optional


class WechatIncomeMsgType(Enum):
    TXT = 1
    PIC = 3
    ON_CONNECT = 5
    HEART_BEAT = 5005
    # 啥都有，包括公众号
    CHAOS = 49

class WechatSendMsgType(Enum):
    TXT = 555
    PIC = 500
    AT = 550
    ATTACH_FILE = 5003

class WechatWebsocketSysMsgType(Enum):
    USER_LIST = 5000
    GET_USER_LIST_SUCCESS = 5001
    GET_USER_LIST_FAIL = 5002
    CHATROOM_MEMBER = 5010
    CHATROOM_MEMBER_NICK = 5020
    PERSONAL_INFO = 6500
    DEBUG_SWITCH = 6000
    PERSONAL_DETAIL = 6550
    DESTROY_ALL = 9999
    # 微信好友请求消息
    NEW_FRIEND_REQUEST = 37
    # 同意微信好友请求消息
    AGREE_TO_FRIEND_REQUEST = 10000

class WechatChaosMsgBaseType(Enum):
    REF = 57


class WechatChaosMsgReferMsgType(Enum):
    TXT = WechatIncomeMsgType.TXT.value
    AT = WechatIncomeMsgType.CHAOS.value

@dataclass
class BaseWechatMessage:
    content: str
    id: str
    srvid: int
    time: str
    type: WechatIncomeMsgType = field(init=False)  # 不由__init__初始化
    _type_value: int = field(default=None, repr=False)  # 存储原始类型值

    def __post_init__(self):
        """初始化后处理类型转换"""
        if self._type_value is not None:
            self.type = WechatIncomeMsgType(self._type_value)

    def __init_subclass__(cls, **kwargs):
        """自动注册子类到消息类型映射表"""
        super().__init_subclass__(**kwargs)

    @classmethod
    def create_from_json(cls, json_str: str) -> 'BaseWechatMessage':
        """智能工厂方法：根据type自动创建对应子类实例"""
        data = json.loads(json_str)
        type_value = data.get('type')

        # 1. 查找对应的子类
        message_class = None
        # message_class = cls._message_registry.get(type_value, None)
        # if message_class is None:
        #     return None
        if type_value == WechatIncomeMsgType.ON_CONNECT.value:
            message_class = OnConnectMessage
        elif type_value == WechatIncomeMsgType.HEART_BEAT.value:
            message_class = HeartBeatMessage
        elif type_value == WechatIncomeMsgType.TXT.value:
            message_class = TextMessage
        else:
            message_class = UnknownMessage

        # 2. 准备数据：重命名type字段
        data_copy = data.copy()
        if 'type' in data_copy:
            data_copy['_type_value'] = data_copy.pop('type')

        # 3. 创建实例
        return message_class(**data_copy)


@dataclass
class SystemMessage(BaseWechatMessage):
    receiver: Optional[str] = None
    sender: Optional[str] = None
    status: Optional[str] = None

@dataclass
class HeartBeatMessage(SystemMessage):
    """心跳消息"""
    MESSAGE_TYPE = WechatIncomeMsgType.HEART_BEAT

@dataclass
class OnConnectMessage(SystemMessage):
    """建立连接消息"""
    MESSAGE_TYPE = WechatIncomeMsgType.ON_CONNECT

@dataclass
class TextMessage(BaseWechatMessage):
    """文本消息"""
    MESSAGE_TYPE = WechatIncomeMsgType.TXT

    id1: Optional[str] = None
    id2: Optional[str] = None
    id3: Optional[str] = None
    other: Optional[str] = None
    wxid: Optional[str] = None

@dataclass
class UnknownMessage(BaseWechatMessage):
    ...

@dataclass
class ReferenceMessageContent:
    """引用消息的内容结构"""
    content: str
    id1: str
    id2: str

    @classmethod
    def from_str(cls, content_str: str) -> 'ReferenceMessageContent':
        """从JSON字符串解析内容"""
        try:
            data = json.loads(content_str)
            return cls(**data)
        except (json.JSONDecodeError, TypeError):
            # 如果不是JSON，创建默认结构
            return cls(content=content_str, id1="", id2="")

@dataclass
class ReferenceSingleLineMessage(SystemMessage):
    """引用消息"""
    MESSAGE_TYPE = WechatIncomeMsgType.CHAOS

    # content字段的特殊处理
    _content_str: str = field(default="", repr=False)
    content_obj: Optional[ReferenceMessageContent] = None

    def __post_init__(self):
        super().__post_init__()
        # 解析content字段
        if self._content_str:
            self.content_obj = ReferenceMessageContent.from_str(self._content_str)

    @property
    def content(self) -> str:
        """content属性的getter"""
        if self.content_obj:
            return self.content_obj.content
        return self._content_str