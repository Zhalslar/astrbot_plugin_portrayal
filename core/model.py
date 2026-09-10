from dataclasses import asdict, dataclass, fields
from typing import Any


@dataclass(slots=True)
class UserProfile:
    user_id: str
    nickname: str = ""
    remark: str = ""

    sex: str = ""
    birthday: str = ""

    phoneNum: str = ""
    eMail: str = ""

    address: str = ""

    long_nick: str = ""

    portrait: str = ""
    timestamp: int = 0
    clone_prompt: str = ""
    # 最近一次修改克隆人格的时间（用于面板提示「画像可能早于当前人格」）
    persona_updated_at: int = 0

    @property
    def persona_id(self) -> str:
        return f"{self.nickname}_{self.user_id}"

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict) -> "UserProfile":
        """从 JSON 还原档案

        只取已知字段：手改过的档案、或将来删掉字段的旧档案都不会让整份加载失败，
        缺失的字段自然落到默认值（兼容老数据）。
        """
        if not isinstance(data, dict):
            raise TypeError("profile data must be a dict")
        allowed = {f.name for f in fields(cls)}
        return cls(**{k: v for k, v in data.items() if k in allowed})

    @classmethod
    def from_qq_data(
        cls,
        user_id: str,
        *,
        data: dict[str, Any],
    ) -> "UserProfile":
        return cls(
            user_id=str(user_id),
            nickname=data.get("nickname", ""),
            remark=data.get("remark", ""),
            sex=data.get("sex", ""),
            birthday=data.get("birthday", ""),
            phoneNum=data.get("phone", ""),
            eMail=data.get("email", ""),
            address=data.get("address", ""),
            long_nick=data.get("long_nick", ""),
        )

    def to_text(self) -> str:
        meta = (
            ("user_id", "QQ号"),
            ("nickname", "昵称"),
            ("remark", "备注"),
            ("sex", "性别"),
            ("birthday", "生日"),
            ("phoneNum", "电话"),
            ("eMail", "邮箱"),
            ("address", "现居"),
            ("long_nick", "签名"),
        )

        lines = [
            f"{label}：{value}"
            for key, label in meta
            if (value := getattr(self, key)) not in ("", None, 0)
        ]

        return "\n".join(lines)
