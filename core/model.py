from dataclasses import asdict, dataclass
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

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict) -> "UserProfile":
        return cls(**data)

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
